/**
 * Human renderings of one served value (`D-1` as amended; decision 32's third allowlisted file).
 *
 * Each export takes exactly one `Leaf` - and, where a rendering needs "now", the dashboard clock -
 * and returns `{ text, exact }`: the words to show, and the leaf's own characters, kept so `Value`
 * can still put the exact wire text in its tooltip and the drawer's Value row. **Never two `Leaf`s**:
 * combining two served values stays `D-1`'s one hard line, unchanged from before this file existed.
 * `tests/unit/humanize.test.ts` pins every form on a fake clock and asserts, from this file's own
 * source, that no export's signature takes a second `Leaf`.
 */
import type { Leaf } from "./wire";

/** What every export returns: the words to show, and the served characters they were built from. */
export type Human = { text: string; exact: string };

const MS_PER_MINUTE = 60_000;
const MINUTES_PER_HOUR = 60;
const SECONDS_PER_MINUTE = 60;

const MONTHS = [
  "Jan",
  "Feb",
  "Mar",
  "Apr",
  "May",
  "Jun",
  "Jul",
  "Aug",
  "Sep",
  "Oct",
  "Nov",
  "Dec",
] as const;

function pad(value: number): string {
  return String(value).padStart(2, "0");
}

/** `hh:mm` on the served instant, in the browser's own local time. */
function clockText(at: Date): string {
  return `${pad(at.getHours())}:${pad(at.getMinutes())}`;
}

/** Whether two instants fall on the same calendar day, in the browser's own local time. */
function sameDay(a: Date, b: Date): boolean {
  return (
    a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate()
  );
}

/**
 * A served ISO timestamp, against the dashboard clock `at`: "2 min ago" (today, within the hour),
 * "16:59" (today, beyond an hour), "24 Sep, 16:59" (an earlier day). The exact wire text always
 * travels with it, for the tooltip and the drawer.
 */
export function humanTimestamp(leaf: Leaf, at: Date): Human {
  const served = new Date(leaf.text);
  const minutesAgo = Math.floor((at.getTime() - served.getTime()) / MS_PER_MINUTE);
  const today = sameDay(served, at);
  const text =
    today && minutesAgo < MINUTES_PER_HOUR
      ? `${minutesAgo} min ago`
      : today
        ? clockText(served)
        : `${served.getDate()} ${MONTHS[served.getMonth()]}, ${clockText(served)}`;
  return { text, exact: leaf.text };
}

/**
 * A served duration in seconds: "0.6 s" verbatim under a minute (no rounding), "2 min 3 s" at or
 * past one minute (whole seconds only, past the minute mark).
 */
export function humanSeconds(leaf: Leaf): Human {
  const whole = Math.floor(Number(leaf.text));
  if (whole < SECONDS_PER_MINUTE) return { text: `${leaf.text} s`, exact: leaf.text };
  const minutes = Math.floor(whole / SECONDS_PER_MINUTE);
  const seconds = whole % SECONDS_PER_MINUTE;
  return { text: `${minutes} min ${seconds} s`, exact: leaf.text };
}

/** A served duration in seconds in words, from the served characters: "0.4 seconds", "1 second". */
export function humanSecondsWords(leaf: Leaf): Human {
  return { text: `${leaf.text} ${leaf.text === "1" ? "second" : "seconds"}`, exact: leaf.text };
}

/** A served instant as `hh:mm` in the browser's own local time, wherever it falls: "Asked 03:12". */
export function humanClock(leaf: Leaf): Human {
  return { text: clockText(new Date(leaf.text)), exact: leaf.text };
}

/**
 * A served ceiling against the dashboard clock `at`: "03:22 (in 8 min)" while it lies ahead, the
 * bare "03:22" once it has passed. Nothing counts down; the caller re-reads the clock once a minute.
 */
export function humanCeiling(leaf: Leaf, at: Date): Human {
  const served = new Date(leaf.text);
  const clock = clockText(served);
  const minutesAhead = Math.ceil((served.getTime() - at.getTime()) / MS_PER_MINUTE);
  return { text: minutesAhead > 0 ? `${clock} (in ${minutesAhead} min)` : clock, exact: leaf.text };
}

/**
 * A served budget in seconds as the words an operator sets it in: "10 min" for a whole number of
 * minutes, "2 min 3 s" beyond one, "45 s" under it.
 */
export function humanBudgetSeconds(leaf: Leaf): Human {
  const whole = Math.floor(Number(leaf.text));
  const minutes = Math.floor(whole / SECONDS_PER_MINUTE);
  const seconds = whole % SECONDS_PER_MINUTE;
  const text =
    minutes === 0 ? `${seconds} s` : seconds === 0 ? `${minutes} min` : `${minutes} min ${seconds} s`;
  return { text, exact: leaf.text };
}

/** A served dollar amount with its currency sign and every served digit: "$0.50". */
export function humanDollars(leaf: Leaf): Human {
  return { text: `$${leaf.text}`, exact: leaf.text };
}

const HOURS_PER_DAY = 24;

/**
 * A served ISO timestamp as how long ago it was, against the dashboard clock `at`: "3 min ago",
 * "2 h ago", "4 d ago". The exact wire text travels with it, for the tooltip and the drawer.
 */
export function humanAgo(leaf: Leaf, at: Date): Human {
  const minutes = Math.max(0, Math.floor((at.getTime() - new Date(leaf.text).getTime()) / MS_PER_MINUTE));
  const hours = Math.floor(minutes / MINUTES_PER_HOUR);
  const days = Math.floor(hours / HOURS_PER_DAY);
  const text = hours === 0 ? `${minutes} min ago` : days === 0 ? `${hours} h ago` : `${days} d ago`;
  return { text, exact: leaf.text };
}

/** The prefix the gateway puts on a source it wrote for a consolidation batch. */
const CONSOLIDATION_PREFIX = "consolidation:";

/**
 * A consolidated fact's source in words: "From consolidation, 2 h ago". `ago` is the fact's own
 * `humanAgo`, already made from its one `created_at` leaf; the only test on `source` is its served
 * prefix, and the batch id is never parsed. The exact `source` travels as `exact`, for the tooltip
 * and the drawer. Any other source answers `undefined` and renders as served.
 */
export function humanFromConsolidation(source: Leaf, ago: Human): Human | undefined {
  if (!source.text.startsWith(CONSOLIDATION_PREFIX)) return undefined;
  return { text: `From consolidation, ${ago.text}`, exact: source.text };
}

/** `V6-5`: "fact" for the served string "1", else "facts" - a lookup on the characters, never a count. */
export function pluralOf(leaf: Leaf, singular: string): string {
  return leaf.text === "1" ? singular : `${singular}s`;
}
