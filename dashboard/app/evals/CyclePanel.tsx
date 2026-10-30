"use client";

/**
 * One improvement cycle, read back step by step (FR-57, UJ-8).
 *
 * Every figure is a leaf of this panel's own response and carries the path it was read from, so the
 * score the promoter decided on is one Tab from `/improve/cycles/{id} -> .score_before` (`D-4`).
 * Nothing is compared here and nothing is subtracted: the decision is `improve/promoter.py`'s and
 * arrives as `rule`, which `RuleBadge` prints verbatim with its static gloss (MUST rules 1, 21).
 *
 * **An undecided cycle is six explained absences, not six zeroes.** FR-46 makes the gateway send
 * `null` in `score_before`, `score_after`, `pass_count_before`, `pass_count_after`, `regressions`
 * and `soft_drops` where no outcome was reached, because a `0` there is a scoring convention and
 * not a measurement. So each reads `unknown` with `lib/null-reasons.ts`'s "no decision reached"
 * beside it, and `rule: ""` says the same thing through `RuleBadge`. A baseline-only cycle is the
 * half case: `score_before` is a measurement and shows as sent, `score_after` reads `unknown`.
 *
 * The reason is only ever looked up after the leaf says it is a null. Asking for one beside a
 * decided cycle's score would print "no decision reached" next to the decision that was reached,
 * which is ticket 104's trap.
 *
 * **`candidate` is a fact, not a verdict.** `candidate: false` means the reflector produced nothing
 * to measure, so the page says that in words beside the field rather than drawing it as a failure.
 */
import { PanelFrame } from "../../components/PanelFrame";
import { PassMark } from "../../components/PassMark";
import { RuleBadge } from "../../components/RuleBadge";
import { ScorePair } from "../../components/ScorePair";
import { StatTile } from "../../components/StatTile";
import { Value } from "../../components/Value";
import type { Wire } from "../../lib/wire";

import { reasonIf } from "./EvalRunsPanel";
import { ProviderLine } from "./ProviderLine";
import { rowsOf } from "../../lib/rows";
import { cycleRoute } from "./limit";
import styles from "./improve.module.css";

/** The panel's name, so a test and a screen reader find it by the same words. */
export const PANEL_TITLE = "Improvement cycle";

/** What the page says where the reflector produced nothing to measure (FR-46). */
export const NO_CANDIDATE = "the reflection produced no candidate";

/** What it says instead of an empty list, where the cycle recorded no step at all. */
export const NO_STEPS = "the ledger recorded no step for this cycle";

/** How the wire spells a false boolean; this tree compares characters, never parsed values. */
const FALSE = "false";

/** The four counts FR-46 may send as `null`, each with its own documented sentence. */
const COUNTS: readonly { label: string; field: string }[] = [
  { label: "passes before", field: "pass_count_before" },
  { label: "passes after", field: "pass_count_after" },
  { label: "regressions", field: "regressions" },
  { label: "soft drops", field: "soft_drops" },
];

type Props = {
  /** The id the URL carried, passed to the gateway exactly as it arrived. */
  improvementRunId: string;
};

export function CyclePanel({ improvementRunId }: Props) {
  const route = cycleRoute(improvementRunId);

  return (
    <PanelFrame route={route} title={PANEL_TITLE} sourceLabel="Ledger: all time">
      {(wire) => (
        <div className={styles.body}>
          <div className={styles.identity}>
            <Value leaf={wire.at(".improvement_run_id")} />
            <Value leaf={wire.at(".suite")} />
            {candidateId(wire)}
          </div>
          <ProviderLine wire={wire} route={route} />
          <div className={styles.marks}>
            <PassMark leaf={wire.at(".candidate")} />
            <PassMark leaf={wire.at(".promoted")} />
            <PassMark leaf={wire.at(".dry_run")} />
            <PassMark leaf={wire.at(".resumed")} />
          </div>
          {wire.at(".candidate").text === FALSE ? (
            <p className={styles.note} data-testid="no-candidate">
              {NO_CANDIDATE}
            </p>
          ) : null}
          <div className={styles.decision} data-testid="decision">
            <RuleBadge leaf={wire.at(".rule")} />
            <ScorePair before={wire.at(".score_before")} after={wire.at(".score_after")} />
          </div>
          <div className={styles.tiles}>
            {COUNTS.map((count) => (
              <StatTile
                key={count.field}
                label={count.label}
                leaf={wire.at(`.${count.field}`)}
                nullReason={reasonIf(wire.at(`.${count.field}`), route, `.${count.field}`)}
              />
            ))}
            <StatTile label="scored" leaf={wire.at(".scored")} />
          </div>
          {steps(wire)}
        </div>
      )}
    </PanelFrame>
  );
}

/**
 * The candidate the cycle recorded, where it recorded one. An undecided cycle sends `cand_id: ""`,
 * and an empty figure is a blank on the screen where the sentence beside `candidate: false`
 * already says what happened, so the figure is not drawn rather than drawn empty.
 */
function candidateId(wire: Wire) {
  const leaf = wire.at(".cand_id");
  return leaf.text === "" ? null : <Value leaf={leaf} />;
}

/**
 * Every step the cycle recorded, in the order the response sent them. The walk asks the wire for
 * the next step until there is none, so the count is the response's and never an arithmetic of
 * this page's (decision 32).
 */
function steps(wire: Wire) {
  const rows = rowsOf(wire, (index) => `.steps[${index}]`);
  if (rows.length === 0) {
    return (
      <p className={styles.note} data-testid="steps-empty">
        {NO_STEPS}
      </p>
    );
  }

  return (
    <ol className={styles.steps} data-testid="steps">
      {rows.map((row) => (
        <li key={row} className={styles.step}>
          <Value leaf={wire.at(`.steps[${row}]`)} />
        </li>
      ))}
    </ol>
  );
}
