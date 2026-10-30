/**
 * The state the Memory pages own, and the routes they read (FR-58, UJ-10).
 *
 * One thing is state here and it lives in a URL: the search `q` that the Facts section of `/memory` was asked
 * with. Everything else is an id a URL already carries - a `mem_id` on the fact page, a session id
 * on the episodes page - so a reload and a shared link show the same screen the operator was
 * looking at, and nothing is stored anywhere (`D-6`).
 *
 * Every value here is text and stays text. A query is spelled into a route exactly as it was
 * typed, because whether an FTS expression is servable is the gateway's answer to give; an id is
 * spelled back verbatim, with no trim, no split and no re-casing, because the gateway is the only
 * thing that takes an id apart (MUST rule 25).
 *
 * The commands are static copy and no id is assembled into one: `<text>` and `<cand_id>` stay
 * placeholders, exactly as `lib/cli-twins.ts` keeps its own, because a command this page filled in
 * would be this page building an id.
 */

/** The gateway route for the active procedure set, as the route table spells it. */
export const PROCEDURES_ROUTE = "/v1/memory/procedures";

/** The gateway route for a facts search, carrying the query exactly as it was typed. */
export function factsRoute(q: string): string {
  return `/v1/memory/facts?q=${q}`;
}

/** The page URL a search lives in, which is what makes it reload-safe and shareable. */
export function factsHref(q: string): string {
  return `/memory?q=${q}`;
}

/** The gateway route for one fact, archived or not, with the id as the URL carried it. */
export function factRoute(memId: string): string {
  return `/v1/memory/facts/${memId}`;
}

/**
 * Where a fact's own `mem_id` opens, taken verbatim from the response that sent it. The id rides
 * in `?id=` beside the page's own `?q=` (FR-64, decision 45), and URL-encoding is the one thing
 * done to it (MUST rule 25).
 */
export function factHref(memId: string): string {
  return `/memory?id=${encodeURIComponent(memId)}`;
}

/** The gateway route for one session's episodes, with the session id as it was typed. */
export function episodesRoute(sessionId: string): string {
  return `/v1/memory/episodes/${sessionId}`;
}

/**
 * Where a pasted session id opens, with no trim, no split and no re-casing. The id rides in
 * `?session=`, the parameter `/sessions` spells the same id kind with (FR-64, decision 45), and
 * URL-encoding is the one thing done to it (MUST rule 25).
 */
export function episodeHref(sessionId: string): string {
  return `/memory?session=${encodeURIComponent(sessionId)}`;
}

/**
 * What the Facts section of `/memory` says while its URL carries no query, taken verbatim from `ux/design.md`.
 * It is a prompt and never "0 facts": the gateway serves `[]` without a `q`, so a count here would
 * report an empty store where nothing was ever asked (`[GAP G-14]`).
 */
export const NEEDS_QUERY = "type a search; the gateway lists nothing without one";

/**
 * What `/memory` says while its URL carries no session id: the same prompt treatment
 * the Facts section of `/memory` gives a missing query (`ux/design.md`, "The six re-addressed pages"). There is
 * no listing of episodes to fall back to - the gateway serves them for one session id and for
 * nothing else - so the bare page is a prompt and never an empty table.
 */
export const NEEDS_SESSION = "paste a session id; the gateway serves episodes for one session at a time";

/**
 * What a search with no hit says. It names the query rather than the store, because a query that
 * matched nothing says nothing at all about how many facts the ledger holds.
 */
export function noMatch(q: string): string {
  return `no active fact matches ${q}`;
}

/** What puts a fact in semantic memory, shown where a search found none (`D-3`). */
export const ADD_COMMAND = "harnessed memory add <text>";

/** What puts a procedure in the active set, shown where the set is empty (`D-3`). */
export const PROMOTE_COMMAND = "harnessed improve promote <cand_id>";

/** What writes an episode, shown where a session wrote none (`D-3`). */
export const RUN_COMMAND = "harnessed run";
