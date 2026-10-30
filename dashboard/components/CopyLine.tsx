"use client";

/**
 * One command to copy, kept as text beside the button that copies it (design.md: "the copy line is
 * a button"). The line is built by the caller from served values; nothing is assembled here, and
 * a browser that refuses the clipboard simply leaves the text to be selected.
 */
import styles from "./CopyLine.module.css";

type Props = {
  /** The command, exactly as it is shown and copied. */
  line: string;
  /** The button's own words: "Copy", "Copy id", "Copy diff command". */
  label: string;
  /** Hook for tests and evidence; names the line, not the button. */
  testId?: string;
};

export function CopyLine({ line, label, testId }: Props) {
  return (
    <div className={styles.line}>
      <code className={styles.code} data-testid={testId}>
        {line}
      </code>
      <button type="button" className={styles.copy} onClick={() => copy(line)}>
        {label}
      </button>
    </div>
  );
}

/** Put the line on the clipboard. A browser that refuses simply leaves the text to be selected. */
export function copy(line: string): void {
  void globalThis.navigator?.clipboard?.writeText(line);
}
