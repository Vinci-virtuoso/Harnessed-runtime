/**
 * The pieces of the Traces page's URL, and the one sentence it says about them (FR-59, FR-64).
 *
 * `?limit=` is the operator's choice, not a gateway figure: he asks for at most this many rows
 * and the gateway answers with however many it has, in its own order. So the limit lives in the
 * URL and nowhere else - no component state that survives a navigation, no storage (`D-6`) - and
 * `/traces` is a pure function of the URL it was served at.
 *
 * The values are text, never numbers, for the same reason every figure in this tree is text: a
 * limit is spelled into a route and printed in a sentence, and it is never calculated with
 * (decision 32). A limit the URL holds that is none of these three is passed to the gateway
 * unchanged, because whether a limit is servable is the gateway's answer to give and not this
 * page's to guess: the route table records `LimitError` as its 400.
 *
 * A session id is spelled into a route and into a page URL **verbatim**, here and nowhere else.
 * Nothing in this file splits, trims or reformats one: it is the id a response field carried or
 * the characters an operator typed, and the gateway is the only thing that takes it apart
 * (MUST rules 15, 25).
 */

/** The three the control offers, in the order it offers them (`experience.md`, route map). */
export const LIMITS: readonly string[] = ["20", "50", "100"];

/** What `/traces` shows when the URL names no limit. */
export const DEFAULT_LIMIT = "20";

/** What an operator runs to put a row in an empty listing (`EmptyState`, `D-3`). */
export const RUN_COMMAND = "harnessed run";

/** The gateway route for the sessions listing at a limit, as the route table spells it. */
export function listingRoute(limit: string): string {
  return `/v1/sessions?limit=${limit}`;
}

/** The gateway route for the runs listing at a limit, as the route table spells it. */
export function runsListingRoute(limit: string): string {
  return `/v1/runs?limit=${limit}`;
}

/** The page URL for a limit, which is where the choice is kept; one limit drives both listings. */
export function listingHref(limit: string): string {
  return `/traces?limit=${limit}`;
}

/** The replay route for one session id, with the id placed in it verbatim (MUST rule 25). */
export function eventsRoute(sessionId: string): string {
  return `/v1/sessions/${sessionId}/events`;
}

/**
 * One session's page, at the id a row carried or an operator typed, unedited. The id rides in
 * `?session=` rather than a path segment (FR-64, decision 45), and URL-encoding is the one thing
 * done to it (MUST rule 25).
 */
export function sessionHref(sessionId: string): string {
  return `/traces?session=${encodeURIComponent(sessionId)}`;
}

/**
 * One run's page, at the key a row carried, unedited (FR-64, decision 45). The key rides in
 * `?key=` rather than a path segment, and URL-encoding is the one thing done to it: a case run
 * key's colons survive it whole, and nothing is trimmed, split or re-cased (MUST rule 25).
 */
export function runHref(runKey: string): string {
  return `/traces?key=${encodeURIComponent(runKey)}`;
}

/**
 * The line under the table: the list is capped, it is newest first, and the order is the
 * gateway's. It says "up to" because the cap is a ceiling the operator asked for and not a count
 * of what arrived - counting the rows rendered would be this page deriving a figure the response
 * never sent.
 */
export function footerLine(limit: string): string {
  return `showing up to ${limit}, newest first, as the gateway ordered them`;
}
