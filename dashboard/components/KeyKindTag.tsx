"use client";

/**
 * `key_kind`, in human words from `HUMAN_WORDS` (`D-7`, FR-49, FR-129; design.md inventory).
 *
 * From v5, `session` and `case_run` render as "Chat" and "Eval case" - `HUMAN_WORDS`'s own two
 * rows for this field - with the ledger's own word kept in `Value`'s tooltip and drawer, so an
 * operator grepping the ledger for `case_run` still finds it there, verbatim (`D-1`, `D-7`). The
 * tag carries no colour coding: a colour would be a second vocabulary to learn and the words
 * already say what they mean.
 */
import { humanWord } from "../lib/human-words";
import type { Leaf } from "../lib/wire";

import { Value } from "./Value";
import styles from "./KeyKindTag.module.css";

type Props = {
  leaf: Leaf;
  /** `-1` where the surrounding row is already one Tab stop (decision 103). */
  tabIndex?: -1;
};

export function KeyKindTag({ tabIndex, leaf }: Props) {
  const word = leaf.kind === "null" ? undefined : humanWord("key_kind", leaf.text);
  const human = word === undefined ? undefined : { text: word, exact: leaf.text };

  return (
    <span className={styles.tag}>
      <Value tabIndex={tabIndex} leaf={leaf} human={human} />
    </span>
  );
}
