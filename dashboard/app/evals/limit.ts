/**
 * The state `/evals` owns, and the sentences it says about it (FR-56).
 *
 * Two pieces, and both of them live in a URL. `?limit=` is the operator's ceiling on how many eval
 * runs he asked for, carried by this page's own URL; the before and after of a comparison are two
 * eval run ids carried by `/evals?before=&after=`'s URL. Neither is ever stored (`D-6`), so a reload and
 * a shared link show the same screen the operator was looking at.
 *
 * The values are text, never numbers, for the same reason every figure in this tree is text: a
 * limit is spelled into a route and printed in a sentence and is never calculated with (decision
 * 32), and an eval run id is characters the gateway sent, spelled back verbatim (MUST rule 25).
 * A limit the URL holds that is none of the three offered is passed to the gateway unchanged,
 * because whether a limit is servable is the gateway's answer to give: `LimitError` is its 400.
 */

/** The three the control offers, in the order it offers them (`experience.md`, route map). */
export const LIMITS: readonly string[] = ["20", "50", "100"];

/** What `/evals` shows when the URL names no limit. */
export const DEFAULT_LIMIT = "20";

/** The gateway route for a limit, as the route table spells it. */
export function listingRoute(limit: string): string {
  return `/v1/eval/runs?limit=${limit}`;
}

/** The page URL for a limit, which is where the choice is kept. */
export function listingHref(limit: string): string {
  return `/evals?limit=${limit}`;
}

/**
 * One eval run's page, at the id a response field spelled, unedited (FR-64, decision 45). The id
 * rides in `?id=` rather than a path segment, and URL-encoding is the one thing done to it
 * (MUST rule 25).
 */
export function evalRunHref(evalRunId: string): string {
  return `/evals?id=${encodeURIComponent(evalRunId)}`;
}

/**
 * Where a picked pair opens. Both ids arrive here exactly as the listing's own `eval_run_id`
 * fields spelled them: nothing is trimmed, split or re-cased on the way into the query string,
 * because an id this page edited would be an id this page built (MUST rule 25). Ticket 108 reads
 * them back off that URL, which is what makes a comparison reload-safe and shareable.
 */
export function compareHref(before: string, after: string): string {
  return `/evals?before=${before}&after=${after}`;
}

/**
 * The line under the table: the list is capped, it is newest first, and the order is the
 * gateway's. It says "up to" because the cap is a ceiling and not a count - the gateway may hold
 * fewer rows than were asked for, and counting the rows rendered would be this page deriving a
 * figure the response never sent.
 */
export function footerLine(limit: string): string {
  return `showing up to ${limit}, newest first, as the gateway ordered them`;
}

/*
 * The Improvement section's share of this page's state (FR-57, UJ-8). It has no limit of its own:
 * the one `?limit=` above drives the promotions listing too. An improvement run id rides in
 * `?cycle=` (FR-64, decision 45), is characters the operator pasted, and is spelled back verbatim
 * (MUST rule 25). The commands are static copy, and no id is assembled into one: `<cand_id>` stays
 * a placeholder, because a command this page filled in would be this page building an id.
 */

/** The gateway route for the promotions listing at a limit. */
export function promotionsRoute(limit: string): string {
  return `/v1/promotions?limit=${limit}`;
}

/** The gateway route for one improvement cycle, with the id exactly as the URL carried it. */
export function cycleRoute(improvementRunId: string): string {
  return `/v1/improve/cycles/${improvementRunId}`;
}

/** Where a pasted improvement run id opens, with URL-encoding the one thing done to it. */
export function cycleHref(improvementRunId: string): string {
  return `/evals?cycle=${encodeURIComponent(improvementRunId)}`;
}

/** The line under the promotions table: capped, in decided order, the gateway's order. */
export function promotionsFooterLine(limit: string): string {
  return `showing up to ${limit}, in decided order, as the gateway ordered them`;
}

/** What puts a promotion row in the ledger, shown where the ledger holds none (`D-3`). */
export const RUN_COMMAND = "harnessed improve run";

/** What prints the improvement run id there is no listing to click through (`[GAP G-8]`). */
export const RUN_JSON_COMMAND = "harnessed improve run --json";

/** What promotes a candidate. The dashboard shows it; it never runs it (`D-8`). */
export const PROMOTE_COMMAND = "harnessed improve promote <cand_id>";
