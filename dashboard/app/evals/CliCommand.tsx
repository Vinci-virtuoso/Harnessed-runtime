"use client";

/**
 * A command the operator runs in his own terminal, offered where a write would be tempting
 * (`D-8`, FR-57).
 *
 * The Improvement pages are the two screens where an operator most wants a button: a candidate is
 * sitting there undecided, and promoting it is one call away. There is no such button anywhere in
 * this dashboard - every action it offers is read-only (refetch, open, copy) - so the temptation is
 * answered with the command itself and a way to put it on the clipboard. The CLI stays primary.
 *
 * The command is static copy with its placeholder left in it. Filling `<cand_id>` in from a row
 * would be this page assembling an id, which is MUST rule 25's anti-pattern, and it is not a
 * figure either: it carries no route and no path, because no response spelled it.
 */
import styles from "./improve.module.css";

/** What the one control here says. It copies; it does not run anything. */
export const COPY_LABEL = "Copy command";

type Props = {
  /** Why this command is here, in the page's own words. */
  note: string;
  /** The command, exactly as `improve_cmd.py` spells it, placeholders included. */
  command: string;
};

export function CliCommand({ note, command }: Props) {
  return (
    <div className={styles.command} data-testid="cli-command">
      <span className={styles.note}>{note}</span>
      <code className={styles.code} data-testid="cli-command-text">
        {command}
      </code>
      <button type="button" className={styles.copy} onClick={() => copy(command)}>
        {COPY_LABEL}
      </button>
    </div>
  );
}

/** Put the line on the clipboard. A browser that refuses simply leaves the text to be selected. */
function copy(line: string): void {
  void globalThis.navigator?.clipboard?.writeText(line);
}
