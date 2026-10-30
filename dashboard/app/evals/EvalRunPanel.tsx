"use client";

/**
 * One eval run, read back field by field and case by case (FR-56, UJ-9).
 *
 * Every figure is a leaf of this panel's own response and carries the path it was read from, so a
 * case's score is one Tab from `/eval/runs/{id} -> .cases[3].score` (`D-4`). Nothing is summed and
 * nothing is compared: the pass count is the one the ledger holds, not a count of the ticks on
 * screen, and a difference between two runs is a different route's answer (FR-62, ticket 108).
 *
 * **A run with no cases is one explained `unknown`.** FR-47 makes the emitters send `score: null`
 * where `case_count` is 0, because the promoter's fail-closed `0.0` is a scoring convention and
 * not a measurement. So the score reads `unknown` with "no evidence: 0 cases", the run's own
 * fields stay readable beside it, and the case table is replaced by the sentence saying there was
 * nothing to table - never an empty grid that looks like a run whose cases all vanished.
 *
 * **`replayed` is a tag, not a verdict.** A case replayed from the ledger was not re-run, which is
 * a fact about where the score came from; `passed` is the evaluator's verdict and is the only
 * boolean here drawn as a pass or a fail (`PassMark`'s own rule). `resumed` and `fresh` are shown
 * as sent for the same reason.
 */
import { DataTable } from "../../components/DataTable";
import { PanelFrame } from "../../components/PanelFrame";
import { PassMark } from "../../components/PassMark";
import { StatTile } from "../../components/StatTile";
import { Value } from "../../components/Value";
import type { Wire } from "../../lib/wire";

import { rowsOf } from "../../lib/rows";
import { reasonIf } from "./EvalRunsPanel";
import { ProviderLine } from "./ProviderLine";
import styles from "./evals.module.css";

/** The tag a case carries where the ledger says its score was replayed rather than measured. */
export const REPLAYED_TAG = "replayed from ledger";

/** What the panel says instead of an empty table, where the run scored nothing at all. */
export const NO_CASES = "the ledger recorded no case for this eval run";

/** How the wire spells a true boolean; this tree compares characters, never parsed values. */
const TRUE = "true";

/** The path the score's documented absence is keyed on. */
const SCORE_PATH = ".score";

type Props = {
  /** The id the URL carried, passed to the gateway exactly as it arrived. */
  evalRunId: string;
};

export function EvalRunPanel({ evalRunId }: Props) {
  const route = `/v1/eval/runs/${evalRunId}`;

  return (
    <PanelFrame route={route} title="Eval run" sourceLabel="Ledger: all time">
      {(wire) => (
        <div className={styles.body}>
          <div className={styles.identity}>
            <Value leaf={wire.at(".eval_run_id")} />
            <Value leaf={wire.at(".suite")} />
            <Value leaf={wire.at(".label")} />
          </div>
          <ProviderLine wire={wire} route={route} />
          <div className={styles.tiles}>
            <StatTile label="score" leaf={wire.at(SCORE_PATH)} nullReason={scoreReason(wire, route)} />
            <StatTile label="cases" leaf={wire.at(".case_count")} />
            <StatTile label="passes" leaf={wire.at(".pass_count")} />
            <StatTile label="scored" leaf={wire.at(".scored")} />
            <StatTile label="samples" leaf={wire.at(".samples")} />
          </div>
          <div className={styles.marks}>
            <PassMark leaf={wire.at(".resumed")} />
            <PassMark leaf={wire.at(".fresh")} />
          </div>
          {cases(wire)}
        </div>
      )}
    </PanelFrame>
  );
}

/** The zero-evidence sentence, and only where the response really sent a null (ticket 104). */
function scoreReason(wire: Wire, route: string): string | undefined {
  return reasonIf(wire.at(SCORE_PATH), route, SCORE_PATH);
}

/**
 * Every case the run recorded, in the order the response sent them. The walk asks the wire for
 * the next case's id until there is none, so the count is the response's and never an arithmetic
 * of this page's (decision 32).
 */
function cases(wire: Wire) {
  const rows = rowsOf(wire, (index) => `.cases[${index}].case_id`);
  if (rows.length === 0) {
    return (
      <p className={styles.note} data-testid="cases-empty">
        {NO_CASES}
      </p>
    );
  }

  return (
    <DataTable
      caption="Cases, in the order the gateway sent them"
      rows={rows}
      columns={[
        { header: "case id", cell: (row) => <Value leaf={wire.at(`.cases[${row}].case_id`)} /> },
        { header: "score", cell: (row) => <Value leaf={wire.at(`.cases[${row}].score`)} /> },
        { header: "verdict", cell: (row) => <PassMark leaf={wire.at(`.cases[${row}].passed`)} /> },
        { header: "attempt", cell: (row) => <Value leaf={wire.at(`.cases[${row}].attempt`)} /> },
        { header: "replayed", cell: (row) => replayed(wire, row) },
      ]}
    />
  );
}

/**
 * Where the case's score came from: the field as sent, plus the tag where it was replayed. The
 * field is shown either way, so a case the evaluator scored fresh reads as `false` rather than as
 * the absence of a tag, which would leave the operator to infer it.
 */
function replayed(wire: Wire, row: number) {
  const leaf = wire.at(`.cases[${row}].replayed`);
  return (
    <span className={styles.caseCell}>
      <Value leaf={leaf} />
      {leaf.text === TRUE ? (
        <span className={styles.tag} data-testid="replayed-tag">
          {REPLAYED_TAG}
        </span>
      ) : null}
    </span>
  );
}
