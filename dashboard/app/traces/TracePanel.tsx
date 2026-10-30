"use client";

/**
 * The run's trace, turn by turn (FR-55, UJ-7).
 *
 * One turn is one list item, in the order the gateway sent them, holding what was asked, what came
 * back and what each tool did. The request and a tool's arguments are structured JSON, so each
 * renders through `OutputView` as kind `record`; the reply text and a tool's output are model
 * text, kind `text` (FR-127). This endpoint carries no separate `output_kind` field, so the kind a
 * field renders as is this panel's own fixed choice per field - never a parse of what the field
 * holds (MUST rule 32) - and the request, reply, arguments and output stay their own characters:
 * nothing is re-parsed, re-indented or pretty-printed to figures a `Value` renders (decision 31).
 *
 * **`retrieval` is always `null` over HTTP** (`[GAP G-5]`): the gateway assembles the trace without
 * the run's recorded retrieval, so the field says nothing about whether memory was injected. That
 * is exactly why it renders `unknown` with the sentence `lib/null-reasons.ts` keys on this route,
 * and never "no retrieval" - the second would be a claim the gateway never made.
 */
import { OutputView } from "../../components/OutputView";
import { PanelFrame } from "../../components/PanelFrame";
import { PassMark } from "../../components/PassMark";
import { StopReasonTag } from "../../components/StopReasonTag";
import { Value } from "../../components/Value";
import { nullReasonFor } from "../../lib/null-reasons";
import type { Leaf, Wire } from "../../lib/wire";

import { leafIfSent, rowsOf } from "../../lib/rows";
import styles from "./runs.module.css";

/** The two kinds this panel ever serves a field as: JSON structure, or the model's own text. */
const KIND_RECORD = "record";
const KIND_TEXT = "text";

/** A fixed kind for one field: this panel's own choice of presenter, never derived from content. */
function kindLeaf(word: string, of: Leaf): Leaf {
  return { kind: "string", text: word, path: of.path, route: of.route };
}

type Props = {
  /** The key the URL carried, passed to the gateway exactly as it arrived. */
  runKey: string;
};

export function TracePanel({ runKey }: Props) {
  const route = `/v1/runs/${runKey}/trace`;

  return (
    <PanelFrame route={route} title="Trace" sourceLabel="Ledger: this session">
      {(wire) => {
        const turns = rowsOf(wire, (index) => `[${index}].iteration`);
        if (turns.length === 0) return null;
        return (
          <ol className={styles.turns}>
            {turns.map((turn) => (
              <li key={turn} className={styles.turn} data-testid="trace-turn">
                {turnBody(wire, route, turn)}
              </li>
            ))}
          </ol>
        );
      }}
    </PanelFrame>
  );
}

/** One iteration: when it ran, what it retrieved, what it asked, what it got, what it called. */
function turnBody(wire: Wire, route: string, turn: number) {
  const retrieval = leafIfSent(wire, `[${turn}].retrieval`);

  return (
    <>
      <div className={styles.turnHead}>
        <span className={styles.term}>iteration</span>
        <Value leaf={wire.at(`[${turn}].iteration`)} />
        <span className={styles.term}>at</span>
        <Value leaf={wire.at(`[${turn}].at`)} />
        <span className={styles.term}>run id</span>
        <Value leaf={wire.at(`[${turn}].run_id`)} />
      </div>
      {retrieval === undefined ? null : (
        <div className={styles.turnRow}>
          <span className={styles.term}>retrieval</span>
          <Value leaf={retrieval} nullReason={nullReasonFor(route, `[${turn}].retrieval`)} />
        </div>
      )}
      <div className={styles.turnRow}>
        <span className={styles.term}>request</span>
      </div>
      <OutputView kind={kindLeaf(KIND_RECORD, wire.at(`[${turn}].request`))} output={wire.at(`[${turn}].request`)} />
      <div className={styles.turnRow}>
        <span className={styles.term}>response</span>
        <StopReasonTag leaf={wire.at(`[${turn}].response.stop_reason`)} />
      </div>
      <OutputView
        kind={kindLeaf(KIND_TEXT, wire.at(`[${turn}].response.content`))}
        output={wire.at(`[${turn}].response.content`)}
      />
      {toolResults(wire, turn)}
    </>
  );
}

/** Every tool the turn called, in the order it recorded them, each with its own `ok`. */
function toolResults(wire: Wire, turn: number) {
  const calls = rowsOf(wire, (index) => `[${turn}].tool_results[${index}].call_id`);
  if (calls.length === 0) {
    return (
      <p className={styles.note}>The turn recorded no tool call.</p>
    );
  }

  return (
    <ul className={styles.calls}>
      {calls.map((call) => (
        <li key={call} className={styles.call} data-testid="tool-result">
          <div className={styles.turnHead}>
            <Value leaf={wire.at(`[${turn}].tool_results[${call}].tool`)} />
            <Value leaf={wire.at(`[${turn}].tool_results[${call}].call_id`)} />
            <PassMark leaf={wire.at(`[${turn}].tool_results[${call}].ok`)} />
            {unparseable(wire, turn, call)}
          </div>
          <div className={styles.turnRow}>
            <span className={styles.term}>arguments</span>
          </div>
          <OutputView
            kind={kindLeaf(KIND_RECORD, wire.at(`[${turn}].tool_results[${call}].arguments`))}
            output={wire.at(`[${turn}].tool_results[${call}].arguments`)}
          />
          <div className={styles.turnRow}>
            <span className={styles.term}>output</span>
          </div>
          <OutputView
            kind={kindLeaf(KIND_TEXT, wire.at(`[${turn}].tool_results[${call}].output`))}
            output={wire.at(`[${turn}].tool_results[${call}].output`)}
          />
        </li>
      ))}
    </ul>
  );
}

/**
 * Whether the tool's arguments could be read at all. It is a separate fact from `ok` - a call can
 * fail on arguments that parsed perfectly - so it is shown as its own field with its own word,
 * never folded into the pass mark beside it.
 */
function unparseable(wire: Wire, turn: number, call: number) {
  const leaf = leafIfSent(wire, `[${turn}].tool_results[${call}].arguments_unparseable`);
  return leaf === undefined ? null : <PassMark leaf={leaf} />;
}
