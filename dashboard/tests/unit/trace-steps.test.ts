/**
 * `lib/trace-steps.ts` (FR-129, decision 68): every row in `TRACE_STEPS`, read against a payload
 * shaped the way the real publisher shapes it (`src/harnessed/**`, cited per event below), so a
 * sentence that names a field the real event never carries fails here rather than only at runtime
 * against a live session. `tests/test_dashboard_vocabulary.py` is the Python-side half of this
 * completeness claim (every `events/names.py::NAMES` entry has a row, or is in `VERBATIM`); this
 * file is the TypeScript-side half, that every row actually renders.
 */
import { describe, expect, it } from "vitest";

import { TRACE_STEPS, VERBATIM, optional, traceStepFor } from "../../lib/trace-steps";
import type { Leaf, Wire } from "../../lib/wire";

/** A `Wire` over a flat map of jq path to its rendered text, for one event's own payload. */
function fakeWire(fields: Readonly<Record<string, string>>): Wire {
  const route = "/v1/sessions/run_fake/stream";
  return {
    route,
    text: JSON.stringify(fields),
    byteLength: "0",
    at: (path: string): Leaf => {
      if (!(path in fields)) throw new Error(`no leaf at ${path}`);
      return { kind: "string", text: fields[path], path, route };
    },
  };
}

/** Every character a rendered sentence part contributes, joined for a plain-text assertion. */
function rendered(name: string, payload: Wire): string {
  const row = traceStepFor(name);
  if (row === undefined) throw new Error(`no row for ${name}`);
  return row
    .sentence(payload)
    .map((part) => ("text" in part ? part.text : payload.at(part.figure).text))
    .join("");
}

// One payload per event this table gives a row, shaped the way its real publisher shapes it
// (see this ticket's payload-field research, cited per group below).
const PAYLOADS: Readonly<Record<string, Readonly<Record<string, string>>>> = {
  "run.started": { ".provider": "fake" },
  "run.finished": { ".iterations": "3", ".tool_calls": "0", ".guardrail": "no_more_tool_calls", ".stop_reason": "end_turn", ".retrieved_tokens": "0" },
  "loop.iteration.started": { ".iteration": "1" },
  "loop.iteration.finished": { ".iteration": "1", ".tool_calls": "0" },
  "loop.guardrail.tripped": { ".guardrail": "iteration_limit", ".iterations": "8", ".stop_reason": "", ".scope": "message" },
  "loop.route.decided": {
    ".decision": "0",
    ".hop": "0",
    ".route": "groq_chat",
    ".handler": "groq",
    ".outcome": "routed",
    ".confidence": "0.9300",
    ".confidence_percent": "93%",
    ".memory_kind": "semantic",
  },
  "tool.call.started": { ".call_id": "call_1", ".name": "fs.read" },
  "tool.call.finished": { ".call_id": "call_1", ".name": "fs.read" },
  "tool.call.failed": { ".call_id": "call_1", ".name": "fs.read", ".error": "ENOENT" },
  "retrieval.gate.decided": { ".retrieve": "true", ".failed_open": "false", ".answer": "0.9", ".reason": "" },
  "retrieval.gate.failed_open": { ".reason": "the gate timed out", ".answer": "0.5", ".retrieve": "true" },
  "retrieval.assembled": { ".budget": "800", ".tokens": "120", ".kept": "3", ".dropped": "0" },
  "retrieval.budget.truncated": { ".budget": "800", ".tokens": "900", ".kept": "2", ".dropped": "1" },
  "eval.run.started": { ".eval_run_id": "evl_1", ".suite": "smoke", ".label": "baseline", ".case_count": "5", ".attempt": "1", ".fresh": "true", ".resumed": "false" },
  "eval.case.started": { ".eval_run_id": "evl_1", ".case_id": "case-a", ".attempt": "1" },
  "eval.case.scored": { ".eval_run_id": "evl_1", ".case_id": "case-a", ".attempt": "1", ".score": "1.0", ".passed": "true", ".replayed": "false" },
  "eval.run.finished": { ".eval_run_id": "evl_1", ".suite": "smoke", ".label": "baseline", ".score": "1.0", ".pass_count": "5", ".case_count": "5", ".scored": "5" },
  "improve.run.started": { ".improvement_run_id": "imp_1", ".suite": "smoke", ".case_count": "5", ".resumed": "false", ".dry_run": "false" },
  "improve.step.recorded": { ".improvement_run_id": "imp_1", ".step": "measure", ".key": "imp_1/baseline" },
  "improve.reflection.written": { ".scope": "_reflect", ".source_case_ids": "[]", ".produced_candidate": "true" },
  "improve.candidate.created": { ".cand_id": "cnd_1", ".source_case_ids": "[]", ".provenance": "reflected", ".text": "Be terse." },
  "improve.promotion.decided": { ".cand_id": "cnd_1", ".promoted": "true", ".rule": "strictly_greater", ".score_before": "0.8", ".score_after": "1.0", ".pass_count_before": "4", ".pass_count_after": "5", ".decided_at": "2026-01-01T00:00:00Z" },
  "improve.run.finished": { ".improvement_run_id": "imp_1", ".cand_id": "cnd_1", ".dry_run": "false", ".promoted": "true" },
  "acp.delegation.started": { ".agent": "claude-code", ".acceptance": "ok" },
  "acp.permission.request": { ".agent": "claude-code", ".tool": "write_file" },
  "acp.permission.decision": { ".agent": "claude-code", ".tool": "write_file", ".allowed": "true", ".rule": "trusted", ".warning": "" },
  "acp.delegation.finished": { ".agent": "claude-code", ".status": "succeeded", ".session_id": "acp_1", ".files_changed": "[]" },
  "acp.delegation.cached": { ".task_hash": "abc123", ".status": "succeeded" },
  "acp.tool.called": { ".agent": "claude-code", ".tool_call_id": "call_1", ".kind": "read", ".title": "Read plan.md" },
  "acp.tool.finished": { ".agent": "claude-code", ".tool_call_id": "call_1", ".status": "ok", ".title": "Read plan.md" },
  "task.started": { ".task_id": "tsk_1", ".started_by": "hand", ".schedule_id": "" },
  "task.paused": { ".task_id": "tsk_1", ".pause_reason": "permission", ".permission_id": "perm_1" },
  "task.finished": { ".task_id": "tsk_1", ".state": "done", ".stop_reason": "finished" },
  "memory.consolidation.finished": { ".batch_id": "bat_1", ".status": "done", ".episode_count": "20", ".facts_written": "1", ".facts_archived": "0", ".failure": "" },
  "error.raised": { ".class": "HarnessError", ".message": "boom", ".stop_reason": "raised" },
  "observer.failed": { ".observer": "dashboard", ".event": "run.started", ".event_seq": "1", ".error": "TypeError", ".message": "boom" },
};

describe("TRACE_STEPS covers every payload shape its own publisher actually sends", () => {
  it("has a payload fixture for every row this table declares", () => {
    expect(Object.keys(PAYLOADS).sort()).toEqual(Object.keys(TRACE_STEPS).sort());
  });

  it("renders every row's sentence without reading a field its real payload never carries", () => {
    for (const [name, fields] of Object.entries(PAYLOADS)) {
      const text = rendered(name, fakeWire(fields));
      expect(text.length, `${name}'s sentence`).toBeGreaterThan(0);
    }
  });

  it("gives every row a kind and a status this payload can answer", () => {
    for (const [name, fields] of Object.entries(PAYLOADS)) {
      const row = TRACE_STEPS[name];
      const payload = fakeWire(fields);
      expect(typeof row.kind(payload)).toBe("string");
      expect(typeof row.status(payload)).toBe("string");
    }
  });
});

describe("run.finished's two shapes (run_cmd.py's four call sites collapse to two: with and without `iterations`)", () => {
  it("names the step count only where the payload carries one", () => {
    const withIterations = fakeWire({ ".iterations": "1", ".stop_reason": "end_turn" });
    expect(rendered("run.finished", withIterations)).toBe("Finished in 1 step");

    const plural = fakeWire({ ".iterations": "3", ".stop_reason": "end_turn" });
    expect(rendered("run.finished", plural)).toBe("Finished in 3 steps");

    // `settle_raised` and the uncertain-outcome path publish no `iterations` at all.
    const raised = fakeWire({ ".stop_reason": "raised" });
    expect(rendered("run.finished", raised)).toBe("Finished");
    expect(TRACE_STEPS["run.finished"].kind(raised)).toBe("failed");
    expect(TRACE_STEPS["run.finished"].status(raised)).toBe("failed");
  });
});

describe("eval.run.finished's narrower hand-written shape (ticket 185's own IMPROVE_FRAMES fixture)", () => {
  it("falls back to bare Finished rather than throwing where suite or pass_count is absent", () => {
    // stream-drops.ts's IMPROVE_FRAMES sends only `case_count`, `eval_run_id` and `score` for its
    // inner eval.run.finished - narrower than evals/runner.py's real publisher, deliberately.
    const narrow = fakeWire({ ".case_count": "2", ".score": "1.0" });
    expect(rendered("eval.run.finished", narrow)).toBe("Finished");

    const full = fakeWire({ ".suite": "smoke", ".pass_count": "5", ".case_count": "5" });
    expect(rendered("eval.run.finished", full)).toBe("Finished smoke: 5 of 5 passed");
  });
});

describe("acp.delegation.finished's four shapes", () => {
  it("reads `.agent` and `.status` from every one, and `.reason` only where it was sent", () => {
    const acp = fakeWire({ ".agent": "claude-code", ".status": "succeeded", ".session_id": "s", ".files_changed": "[]" });
    expect(rendered("acp.delegation.finished", acp)).toBe("claude-code finished");

    const timeout = fakeWire({ ".agent": "grok", ".status": "failed", ".reason": "timeout" });
    expect(rendered("acp.delegation.finished", timeout)).toBe("grok failed: timeout");

    const succeededCli = fakeWire({ ".agent": "grok", ".status": "succeeded" });
    expect(rendered("acp.delegation.finished", succeededCli)).toBe("grok finished");
  });
});

describe("optional()", () => {
  it("reads a field a payload carries, and answers undefined for one it does not", () => {
    const payload = fakeWire({ ".stop_reason": "end_turn" });
    expect(optional(payload, ".stop_reason")?.text).toBe("end_turn");
    expect(optional(payload, ".iterations")).toBeUndefined();
  });
});

describe("VERBATIM", () => {
  it("names exactly the three reserved events no publisher under src/ has ever used", () => {
    expect([...VERBATIM].sort()).toEqual([
      "memory.write.committed",
      "provider.request.sent",
      "provider.response.received",
    ]);
    for (const name of VERBATIM) expect(traceStepFor(name)).toBeUndefined();
  });
});

describe("a name neither table holds", () => {
  it("has no row, which is what lets a verbatim fallback render (AC3)", () => {
    expect(traceStepFor("not.a.real.event")).toBeUndefined();
  });
});

describe("the v6 sentences (ticket 254, FR-155)", () => {
  it("words a permission request by the tool kind, and marks it waiting", () => {
    const request = fakeWire({ ".agent": "Claude Code", ".tool": "execute", ".tool_kind": "execute" });
    expect(rendered("acp.permission.request", request)).toBe("Claude Code asks to run a command");
    expect(TRACE_STEPS["acp.permission.request"].status(request)).toBe("running");
    const unknown = fakeWire({ ".agent": "Claude Code", ".tool": "mystery", ".tool_kind": "mystery" });
    expect(rendered("acp.permission.request", unknown)).toBe("Claude Code asks to use mystery");
  });

  it("words each decision by who or what decided", () => {
    const decide = (rule: string, allowed: string) =>
      rendered("acp.permission.decision", fakeWire({ ".agent": "a", ".tool": "t", ".allowed": allowed, ".rule": rule }));
    expect(decide("operator", "true")).toBe("Approved by you");
    expect(decide("operator", "false")).toBe("Denied by you");
    expect(decide("ceiling", "false")).toBe("Denied: no answer in time");
    expect(decide("allowlist", "true")).toBe("Allowed by its settings");
    expect(decide("deny_list", "false")).toBe("Refused by its settings");
  });

  it("words a task's start, pause and finish", () => {
    expect(rendered("task.started", fakeWire({ ".started_by": "schedule" }))).toBe("Task started (a schedule)");
    expect(rendered("task.started", fakeWire({ ".started_by": "hand" }))).toBe("Task started (you, by hand)");
    expect(rendered("task.paused", fakeWire({ ".pause_reason": "permission" }))).toBe(
      "Paused: waiting for your answer to an agent's request",
    );
    expect(rendered("task.paused", fakeWire({ ".pause_reason": "uncertain" }))).toBe("Paused: the router was not sure");
    expect(rendered("task.finished", fakeWire({ ".state": "done", ".stop_reason": "finished" }))).toBe("Task done");
    const failed = fakeWire({ ".state": "failed", ".stop_reason": "budget" });
    expect(rendered("task.finished", failed)).toBe("Task failed: stopped at its budget");
    expect(TRACE_STEPS["task.finished"].status(failed)).toBe("failed");
  });

  it("names the branch of a delegation that carries a repository record", () => {
    const changed = fakeWire({ ".agent": "Claude Code", ".status": "succeeded", ".branch": "main" });
    expect(rendered("acp.delegation.finished", changed)).toBe("Claude Code finished - changed files on main");
  });

  it("words a consolidation, choosing the singular from the served string", () => {
    const many = fakeWire({ ".status": "done", ".facts_written": "4", ".episode_count": "20" });
    expect(rendered("memory.consolidation.finished", many)).toBe("Memory consolidated: 4 facts from 20 episodes");
    const one = fakeWire({ ".status": "done", ".facts_written": "1", ".episode_count": "1" });
    expect(rendered("memory.consolidation.finished", one)).toBe("Memory consolidated: 1 fact from 1 episode");
    const failed = fakeWire({ ".status": "failed", ".facts_written": "0", ".episode_count": "20" });
    expect(rendered("memory.consolidation.finished", failed)).toBe("Memory consolidation failed: wrote nothing");
  });
});
