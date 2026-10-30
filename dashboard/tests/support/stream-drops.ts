/**
 * The dropped-connection sequences FR-59 needs, cut from the recorded SSE frames (decision 33).
 *
 * A gateway cannot be asked to drop a connection halfway through, so the input for "dropped at
 * `seq` N of `total`" has to be made. It is **made by cutting, never by writing**: the recorded
 * `tests/fixtures/gateway/sessions.stream.sse` is split on the blank line that ends a frame, and
 * the pieces are handed back whole. Nothing here edits the recording (MUST rule 24), and a frame
 * that reaches a test is the bytes `gateway/sse.py::frame` produced.
 *
 * The one exception is :data:`IMPROVE_FRAMES`, and it is marked as what it is: **hand-written**.
 * No recording holds an improve session's stream, and decision 33 allows exactly this class of
 * hand-made input for stream tests. The frames follow `frame()`'s four lines exactly - `id:` is
 * the `seq`, `event:` is a name from `events/names.py`, `data:` is one line of canonical JSON -
 * and the sequence is the shape decision 17 describes: an outer `improve.run.started`, an inner
 * eval that starts and finishes on the same bus, then the cycle's own `improve.run.finished`.
 * It exists to prove the panel does not close on the inner finish (MUST rule 20).
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const FIXTURES = resolve(import.meta.dirname, "../fixtures/gateway");

/** A frame ends at a blank line, which is a second newline (decision 20). */
const FRAME_SEPARATOR = "\n\n";

/** The seeded session the whole recorded corpus is about. */
export const SESSION_ID = "run_0000000002NEHPN093CBQMNNV3";

/** The replay route the stream hands off from, as the route table spells it. */
export const EVENTS_ROUTE = `/v1/sessions/${SESSION_ID}/events`;

/** The recorded replay body, byte for byte. */
export const EVENTS_BODY = readFileSync(resolve(FIXTURES, "sessions.events.body"), "utf8");

/**
 * The recorded stream, one whole frame per entry, each still ending in its own blank line, so
 * `join("")` is the recording again character for character.
 */
export const RECORDED_FRAMES: readonly string[] = readFileSync(
  resolve(FIXTURES, "sessions.stream.sse"),
  "utf8",
)
  .split(FRAME_SEPARATOR)
  .filter((block) => block.length > 0)
  .map((block) => `${block}${FRAME_SEPARATOR}`);

/** The `id:` line of each frame, which is the `seq` a row renders. */
export function seqOf(frames: readonly string[]): string[] {
  return frames.map((frame) => frame.slice("id: ".length, frame.indexOf("\n")));
}

/** A connection cut after `seq`: what the client received, and what it must ask for again. */
export function cutAt(seq: string): { upTo: readonly string[]; after: readonly string[] } {
  const seqs = seqOf(RECORDED_FRAMES);
  const at = seqs.indexOf(seq);
  if (at < 0) throw new Error(`the recorded stream has no frame at seq ${seq}`);
  return { upTo: RECORDED_FRAMES.slice(0, at + 1), after: RECORDED_FRAMES.slice(at + 1) };
}

/** The start event at `seq` 1 of an improve session, and the finish decision 17 pairs with it. */
export const IMPROVE_START = "improve.run.started";
export const IMPROVE_TERMINAL = "improve.run.finished";

/** One hand-written frame, in `gateway/sse.py::frame`'s four lines. */
function written(seq: string, name: string, payload: string): string {
  return `id: ${seq}\nevent: ${name}\ndata: ${payload}${FRAME_SEPARATOR}`;
}

/**
 * **Hand-written** (decision 33): an improve cycle's own session, holding an inner eval run that
 * starts and finishes before the cycle does. The inner `eval.run.finished` at `seq` 3 is the one
 * a flat terminal set would close on, and the one MUST rule 20 exists to keep open.
 */
export const IMPROVE_FRAMES: readonly string[] = [
  written("1", IMPROVE_START, '{"improvement_run_id":"imp_000000000ZZZZZZZZZZZZZZZZZ"}'),
  written("2", "eval.run.started", '{"eval_run_id":"evl_000000000YYYYYYYYYYYYYYYYY","suite":"smoke"}'),
  written("3", "eval.run.finished", '{"case_count":2,"eval_run_id":"evl_000000000YYYYYYYYYYYYYYYYY","score":1.0}'),
  written("4", "improve.candidate.proposed", '{"cand_id":"cnd_000000000XXXXXXXXXXXXXXXXX"}'),
  written("5", IMPROVE_TERMINAL, '{"cand_id":"cnd_000000000XXXXXXXXXXXXXXXXX","dry_run":false,"promoted":true}'),
];

/**
 * **Hand-written** (decision 33): the payload shapes `design.md` gives a component of their own
 * and that no seeded session produces - a truncated `stop_reason`, a case verdict either way, and
 * a field the ledger recorded as `null`. `tests/unit/traces.test.tsx` wrote three states for the
 * same reason: the design names a treatment for each, and a recording holds none of them.
 */
export const VOCABULARY_FRAMES: readonly string[] = [
  written("9", "eval.case.finished", '{"case_id":"case-a","passed":true}'),
  written("10", "eval.case.finished", '{"case_id":"case-b","passed":false}'),
  written("11", "run.finished", '{"guardrail":null,"stop_reason":"max_tokens"}'),
  written("12", "run.finished", '{"stop_reason":"end_turn"}'),
  written("13", "run.finished", '{"stop_reason":null}'),
];

/**
 * **Hand-written** (decision 33): the pair `run_cmd.settle_raised` publishes on a run's own bus
 * when its composition raises after the 202 (FR-92) - `error.raised` then `run.finished` with
 * `guardrail: null`, the exact shape `tests/test_control_runs.py` pins. `RECORDED_FRAMES[0]` is
 * the same seeded session's real `run.started`, so the three read as one whole stream from `seq` 1.
 */
export const RAISED_FRAMES: readonly string[] = [
  RECORDED_FRAMES[0],
  written(
    "2",
    "error.raised",
    '{"class":"HarnessError","message":"the composition raised mid-run","stop_reason":"raised"}',
  ),
  written("3", "run.finished", '{"guardrail":null,"stop_reason":"raised"}'),
];
