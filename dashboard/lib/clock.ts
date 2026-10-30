/**
 * The dashboard's own clock, and one of the two files the decision 32 lint allowlists by name.
 *
 * **Nothing here ever touches a gateway value.** Every function reads the machine the browser is
 * on, which is exactly why `D-5` labels what it produces "(dashboard clock)": a panel states when
 * *it* fetched, never when the ledger wrote. A figure is a `Leaf` and is rendered verbatim; a
 * time on this page is this file's and is labelled as such.
 *
 * Fetch time and the five minute stale mark `D-5` sets, both measured on this machine alone.
 */

/** When the browser received something. Taken on receipt, never read out of a response field. */
export function now(): Date {
  return new Date();
}

/** `hh:mm:ss` on the dashboard's clock, for the footer line `D-5` requires. */
export function clockTime(at: Date): string {
  return at.toTimeString().slice(0, "hh:mm:ss".length);
}

/** The mark `D-5` sets: five minutes after the dashboard fetched, a panel is stale. */
export const STALE_AFTER_MINUTES = 5;

const MILLISECONDS_PER_MINUTE = 60 * 1000;
const STALE_AFTER_MS = STALE_AFTER_MINUTES * MILLISECONDS_PER_MINUTE;

/**
 * How often a panel re-reads this clock to see whether it has aged past the mark. A render, never
 * a request: `D-6` says a data route is read on navigation and on `Refetch`, and at no other time.
 */
export const STALE_TICK_MS = MILLISECONDS_PER_MINUTE;

/** Has a panel fetched at `fetchedAt` aged past the mark by `at`? Both are this clock's. */
export function isStale(fetchedAt: Date, at: Date): boolean {
  return at.getTime() - fetchedAt.getTime() >= STALE_AFTER_MS;
}

/**
 * Whole elapsed minutes, as the text a badge renders. A partial minute is never rounded up: a
 * panel four minutes and fifty-nine seconds old says `4`, because it is not yet five minutes old.
 */
export function minutesSince(fetchedAt: Date, at: Date): string {
  return String(Math.floor((at.getTime() - fetchedAt.getTime()) / MILLISECONDS_PER_MINUTE));
}

/** `D-5`'s footer sentence, from v5: "3 min ago" - the words `PanelFrame`'s footer and `Value`'s
 * own tooltip both build "Updated " / "updated " onto (decision 68's `PanelFrame` and `Value` rows). */
export function minutesAgo(fetchedAt: Date, at: Date): string {
  return `${minutesSince(fetchedAt, at)} min ago`;
}
