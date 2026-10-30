"use client";

/**
 * A boolean field, shown as the pill design.md specifies (`D-7`, FR-49; design.md inventory).
 *
 * Only `passed` is a pass or a fail. That word is the evaluator's verdict on a case, so a check
 * glyph and the word "pass" are the faithful rendering of it. Every other boolean is a different
 * fact wearing the same shape: `promoted: false` means the promoter declined, and calling that a
 * "fail" would invent a judgement the ledger never made - the same confusion MUST rule 15 refuses
 * for ids. So every field but `passed` prints its own name and its own value, verbatim.
 *
 * The word is always drawn. The ground colour repeats it and never replaces it, and the glyph is
 * `aria-hidden` so the pill is heard as one word rather than as a symbol and a word.
 */
import type { Leaf } from "../lib/wire";

import { Value } from "./Value";
import styles from "./PassMark.module.css";

/** The one field whose boolean is a verdict, and the two words that verdict is spelled with. */
const VERDICT_FIELD = "passed";
const PASS = "pass";
const FAIL = "fail";

/** How the wire spells a true boolean; this tree compares characters, never parsed values. */
const TRUE = "true";

type Props = {
  leaf: Leaf;
};

export function PassMark({ leaf }: Props) {
  const field = fieldOf(leaf.path);

  if (field !== VERDICT_FIELD) {
    return (
      <span className={styles.mark} data-testid="pass-mark">
        <span className={styles.word}>{`${field}: `}</span>
        <Value leaf={leaf} />
      </span>
    );
  }

  const passed = leaf.text === TRUE;

  return (
    <span
      className={passed ? `${styles.mark} ${styles.pass}` : `${styles.mark} ${styles.fail}`}
      data-testid="pass-mark"
    >
      <span className={styles.glyph} data-testid="pass-mark-glyph" aria-hidden="true">
        {passed ? "✓" : "✗"}
      </span>
      <span className={styles.word} data-testid="pass-mark-word">
        {passed ? PASS : FAIL}
      </span>
    </span>
  );
}

/** The field a jq path ends in: `.cases[0].passed` is `passed`, `.promoted` is `promoted`. */
function fieldOf(path: string): string {
  return path.split(".").at(-1) ?? "";
}
