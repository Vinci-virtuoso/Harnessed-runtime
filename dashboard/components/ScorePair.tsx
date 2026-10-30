"use client";

/**
 * Two read figures with a plain `->` between them (`D-1`, FR-49; design.md inventory).
 *
 * There is no delta. `0.8 -> 1.0` is a rise of `0.2` on one reading and of 25% on another, and
 * both are numbers the gateway never sent: deciding whether a move is an improvement belongs to
 * `improve/promoter.py` and arrives as `rule`, not as an arrow this component colours in
 * (MUST rules 1 and 21). So the join is a hyphen and a greater-than, carrying no direction, and
 * no up or down glyph appears anywhere.
 *
 * Each side is a `Value`, so each keeps its own route, path and source chip, and a `null` side is
 * the `unknown` treatment with its documented reason rather than a zero. The pair announces itself
 * as "score before 0.8, score after 1.0" - the one sentence design.md specifies - so the two
 * figures are heard as a pair rather than as two loose numbers.
 */
import { nullReasonFor } from "../lib/null-reasons";
import type { Leaf } from "../lib/wire";

import { Value } from "./Value";
import styles from "./ScorePair.module.css";

/** The join, spelled as characters and never as an arrow glyph that could carry a direction. */
const JOIN = "->";

/** What an absent side is called, in the figure and in the announcement alike (`D-2`). */
const UNKNOWN = "unknown";

type Props = {
  before: Leaf;
  after: Leaf;
};

export function ScorePair({ before, after }: Props) {
  return (
    <span
      className={styles.pair}
      role="group"
      aria-label={`score before ${spoken(before)}, score after ${spoken(after)}`}
    >
      <Value leaf={before} nullReason={reasonFor(before)} />
      <span className={styles.join} data-testid="score-pair-join" aria-hidden="true">
        {JOIN}
      </span>
      <Value leaf={after} nullReason={reasonFor(after)} />
    </span>
  );
}

/** What this side is called out loud: its own characters, or the one word an absence gets. */
function spoken(leaf: Leaf): string {
  return leaf.kind === "null" ? UNKNOWN : leaf.text;
}

/**
 * The documented sentence for an absent side, and nothing for a present one: the reason table is
 * keyed on route and path alone, so asking it about a measured score would print "no decision
 * reached" beside a decision that was reached.
 */
function reasonFor(leaf: Leaf): string | undefined {
  return leaf.kind === "null" ? nullReasonFor(leaf.route, leaf.path) : undefined;
}
