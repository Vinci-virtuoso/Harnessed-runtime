"use client";

/**
 * The footer's one line of freshness (`D-5`, decision 68): "Updated N min ago", turning
 * `color-warn` past five minutes and nowhere else.
 *
 * It renders the label it is handed and works nothing out for itself: the elapsed minutes are
 * `lib/clock.ts`'s arithmetic on the dashboard's own clock, one of the three files decision 32
 * allowlists for it. The colour is never the only signal - the word "Updated ... ago" already
 * states the fact; `warn` just carries it a second way.
 */
import styles from "./StaleBadge.module.css";

type Props = {
  /** "Updated N min ago", taken verbatim from `lib/clock.ts::minutesAgo`. */
  label: string;
  /** Whether the panel has aged past the five minute mark (`D-5`). */
  warn: boolean;
};

export function StaleBadge({ label, warn }: Props) {
  return (
    <span
      className={warn ? `${styles.badge} ${styles.warn}` : styles.badge}
      data-testid="stale-badge"
      data-warn={warn}
    >
      {label}
    </span>
  );
}
