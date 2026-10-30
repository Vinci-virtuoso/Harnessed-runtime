"use client";

/**
 * `stop_reason`, in human words from `HUMAN_WORDS` (`D-7`, FR-49, FR-129; design.md inventory).
 *
 * A word the table knows renders as its display text, with the ledger's own word kept in `Value`'s
 * tooltip and drawer (`D-1`, `D-7`); a word it does not know - a newer model's own addition -
 * renders verbatim in mono rather than quietly as nothing, exactly as `HUMAN_WORDS`'s own fallback
 * rule says.
 *
 * `max_tokens` means the turn was cut off, which is the one value an operator must not skim past.
 * It gets the warn colour *and* the word "truncated" beside it, in text: the colour is a second
 * carrier of a fact the sentence already states, never the only one.
 *
 * A `null` is the absence of a record, not a reason: it renders `unknown` with the sentence
 * `lib/null-reasons.ts` keys on this route and path ("written by a build older than migration
 * 007"), and never the string `null`.
 */
import { humanWord } from "../lib/human-words";
import type { Leaf } from "../lib/wire";

import { Value } from "./Value";
import styles from "./StopReasonTag.module.css";

/** The word beside `max_tokens`, so truncation is spelled out and not left to a colour. */
export const TRUNCATED = "truncated";

/** The one value whose consequence the operator has to be told in words. */
const MAX_TOKENS = "max_tokens";

type Props = {
  leaf: Leaf;
  /** `-1` where the surrounding row is already one Tab stop (decision 103). */
  tabIndex?: -1;
  /** Why the run recorded no stop reason, where it recorded none (`D-2`). */
  nullReason?: string;
};

export function StopReasonTag({ tabIndex, leaf, nullReason }: Props) {
  const unknown = leaf.kind === "null";
  const truncated = leaf.text === MAX_TOKENS;
  const word = unknown ? undefined : humanWord("stop_reason", leaf.text);
  const human = word === undefined ? undefined : { text: word, exact: leaf.text };

  return (
    <span className={truncated ? `${styles.tag} ${styles.warn}` : styles.tag}>
      <Value tabIndex={tabIndex} leaf={leaf} nullReason={unknown ? nullReason : undefined} human={human} />
      {truncated ? <span className={styles.note}>{TRUNCATED}</span> : null}
    </span>
  );
}
