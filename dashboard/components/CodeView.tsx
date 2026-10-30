"use client";

/**
 * A file, a patch or a diff, verbatim (FR-127, `D-10`). No syntax colour - colour carries meaning,
 * never decoration - so the only marks are a `+` glyph on an added line and a `-` on a removed
 * one, read from the line's own leading character. Line numbers count the served text; nothing
 * here reformats, reindents or truncates a line.
 */
import type { Leaf } from "../lib/wire";

import styles from "./CodeView.module.css";

type Props = {
  /** The code or diff text, whole. */
  output: Leaf;
  /** The filename, where the caller has one. */
  name?: Leaf;
};

const ADDED = "+";
const REMOVED = "-";

function glyphOf(line: string): string | undefined {
  if (line.startsWith(ADDED)) return ADDED;
  if (line.startsWith(REMOVED)) return REMOVED;
  return undefined;
}

export function CodeView({ output, name }: Props) {
  // A dummy element at index 0 makes every real line's own array index its 1-based line number,
  // with no arithmetic (the no-derivation lint bans `+` in this tree, decision 32).
  const padded = ["", ...output.text.split("\n")];

  return (
    <div className={styles.frame} data-testid="code-view">
      {name === undefined ? null : <div className={styles.header}>{name.text}</div>}
      <pre className={styles.body} tabIndex={0}>
        {[...padded.entries()]
          .filter(([lineNumber]) => lineNumber !== 0)
          .map(([lineNumber, line]) => {
            const glyph = glyphOf(line);
            const className =
              glyph === ADDED
                ? `${styles.line} ${styles.added}`
                : glyph === REMOVED
                  ? `${styles.line} ${styles.removed}`
                  : styles.line;
            return (
              <div key={lineNumber} className={className} data-testid="code-line">
                <span className={styles.number} aria-hidden="true">
                  {String(lineNumber)}
                </span>
                <span className={styles.glyph} aria-hidden="true">
                  {glyph ?? ""}
                </span>
                <span>{line}</span>
              </div>
            );
          })}
      </pre>
    </div>
  );
}
