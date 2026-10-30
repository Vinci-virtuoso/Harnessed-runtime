"use client";

/**
 * The deciding `rule` of a promotion, printed as the promoter wrote it, with its meaning beneath
 * (`D-7`, FR-49; design.md inventory).
 *
 * The word is the ledger's: `score`, `tiebreak`, `no_gain`, `regression`, `missing_case`,
 * `undersampled`. The gloss is static copy keyed on that word - the gateway does not serve one
 * (`[GAP G-13]` is Out) - and it is visible text under the badge rather than a tooltip, because a
 * reader who cannot hover is exactly the reader who most needs the sentence.
 *
 * An empty `rule` is the undecided cycle of FR-46: nothing was compared, so there is no word to
 * print and the badge says so in the sentence the design documents. It is not rendered as an
 * empty outline, which would read as a rule whose name failed to load.
 */
import type { Leaf } from "../lib/wire";

import { Value } from "./Value";
import styles from "./RuleBadge.module.css";

/** What an empty `rule` says, verbatim from design.md and `lib/null-reasons.ts`. */
export const UNDECIDED = "no decision reached";

/**
 * One sentence per rule, keyed on the verbatim word. The keys are the promoter's `_REASONS`, which
 * are `evals/scoring.py::RULES` and nothing else, so a rule this table cannot explain shows as a
 * missing gloss rather than as a wrong one.
 */
export const RULE_GLOSSES: Readonly<Record<string, string>> = {
  score: "the aggregate score rose by more than EPSILON and nothing regressed",
  tiebreak:
    "the score did not move, one more case passed, and nothing regressed. Promoted on the pass-count tiebreak, never on score",
  no_gain: "the score condition failed: no gain beyond EPSILON and no pass-count tiebreak",
  regression: "a case that passed before now fails, and the regression condition is not tie-breakable",
  missing_case:
    "a case in the baseline was not scored in the candidate run, so the decision fails closed",
  undersampled:
    "one side or both was drawn fewer times than the declared minimum, so the numbers compared are a draw and not a measurement",
};

type Props = {
  leaf: Leaf;
};

export function RuleBadge({ leaf }: Props) {
  if (leaf.kind === "null" || leaf.text === "") {
    return <span className={styles.undecided}>{UNDECIDED}</span>;
  }

  const gloss = RULE_GLOSSES[leaf.text];

  return (
    <span className={styles.badge}>
      <span className={styles.word}>
        <Value leaf={leaf} />
      </span>
      {gloss === undefined ? null : (
        <span className={styles.gloss} data-testid="rule-gloss">
          {gloss}
        </span>
      )}
    </span>
  );
}
