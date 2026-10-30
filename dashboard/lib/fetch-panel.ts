/**
 * One gateway read, turned into exactly one panel state (decisions 31, 34, 43; MUST rules 22, 23).
 *
 * Every read the dashboard makes goes through here. It reaches the gateway only at the relative
 * `/v1/...` - the process that served the page is the one the gateway is mounted in (decision 43),
 * so there is no second origin to name - only with `GET`, and only with `cache: "no-store"`, so no
 * Next cache and no browser cache ever holds a response (`D-6`). It keeps the raw text and its
 * UTF-8 byte length for the provenance drawer, takes the fetch time from `lib/clock.ts` on
 * receipt, and parses with `parseWire` so a figure is the characters the wire spelled and never a
 * re-serialized one.
 *
 * **Unreachable is a rejected `fetch`, never a status** (MUST rule 23). A browser `fetch` that
 * rejects means the process is not there to answer, and a body read that rejects after the
 * headers arrived - a gateway that dies mid-answer - is the same state. Every non-2xx is the
 * gateway's own: `absent` (404) or `error`, quoting decision 24's `{"error","message"}` body
 * verbatim, and a 2xx whose body is not JSON is `error` under `UnreadableBody` rather than a
 * rejection no panel has a state for.
 */
import { now } from "./clock";
import { WireUnsupported, parseWire, type Wire } from "./wire";

/**
 * The origin every read names: the read gateway at the relative `/v1`, on the origin that served
 * the page (MUST rule 22). A panel never fetches any other origin; `next dev` reaches the
 * operator's gateway through the development rewrite in `next.config.ts`, never through a URL
 * spelled here.
 */
export const GATEWAY_ORIGIN = "/v1";

/** The version segment both the gateway origin and a route spell, so it is not spelled twice. */
const VERSION = GATEWAY_ORIGIN;

/** The status a missing surface answers with. Compared as text: nothing here is a number. */
const NOT_FOUND = "404";

/** A body that is not decision 24's error contract is quoted as itself under this class name. */
const UNREADABLE = "UnreadableBody";

/** The response as it arrived, kept whole for `ProvenanceDrawer` (FR-51) and the panel footer. */
export type RawResponse = {
  /**
   * The request's own URL: the `/v1/...` this read was for, on the origin that served the page.
   * There is no transport URL to tell it from any more (decision 43): the drawer shows this, and
   * its `curl` line is this on the dashboard's origin.
   */
  url: string;
  /** The HTTP status as text, because it is displayed and never calculated (decision 32). */
  status: string;
  /** The body's UTF-8 length, as text, for the same reason. */
  bytes: string;
  /** The raw body, exactly as received. */
  text: string;
  /** When the browser received it, on the dashboard's clock (`D-5`). */
  fetchedAt: Date;
};

/** Exactly one state, with exactly what that state needs to render itself. */
export type PanelResult =
  | { state: "ok"; response: RawResponse; wire: Wire }
  | { state: "absent"; response: RawResponse; error: string; message: string }
  | { state: "error"; response: RawResponse; error: string; message: string }
  | { state: "unreachable"; url: string; cause: string };

/** The gateway URL a route names, built from the origin that already carries `/v1`. */
export function gatewayHref(origin: string, route: string): string {
  if (!route.startsWith(VERSION)) return `${origin}${route}`;
  return `${origin}${route.slice(VERSION.length)}`;
}

/** What a rejected `fetch` can say for itself, reported as itself rather than as a guess. */
function causeOf(thrown: unknown): string {
  if (!(thrown instanceof Error)) return String(thrown);
  return thrown.message ? thrown.message : thrown.name;
}

/** Decision 24's `{"error","message"}`, read through `parseWire` so both are verbatim. */
function errorBody(text: string, route: string): { error: string; message: string } {
  try {
    const wire = parseWire(text, route);
    return { error: wire.at(".error").text, message: wire.at(".message").text };
  } catch (thrown) {
    if (thrown instanceof WireUnsupported) return { error: thrown.name, message: thrown.message };
    return { error: UNREADABLE, message: text };
  }
}

/**
 * Read one route from the gateway and say which state it is.
 *
 * @param route the gateway route, as the route table spells it: `/v1/eval/runs/{id}`
 * @param origin the origin the drawer and the `curl` line name: `GATEWAY_ORIGIN`, from the shell
 * @param signal aborts the read when the panel unmounts or refetches
 */
export async function fetchPanel(
  route: string,
  origin: string,
  signal?: AbortSignal,
): Promise<PanelResult> {
  // The URL the drawer shows and the route the browser fetches are the same `/v1/...` under
  // decision 43: `origin` already is `/v1`, so `gatewayHref` adds nothing a route did not spell.
  const url = gatewayHref(origin, route);

  let received: Response;
  let text: string;
  try {
    received = await fetch(route, { cache: "no-store", signal });
    // The body is read under the same guard, because it can reject where the `fetch` did not: a
    // gateway that dies mid-answer, and a `signal` aborted after the headers arrived, both fail
    // here. Uncaught, that rejection leaves the panel on its skeleton for ever - no state, no
    // `Refetch` - which is the one thing this module exists to prevent.
    text = await received.text();
  } catch (thrown) {
    return { state: "unreachable", url, cause: causeOf(thrown) };
  }
  // The clock is read on receipt, so the fetch time is when the browser got the bytes (`D-5`).
  const response: RawResponse = {
    url,
    status: String(received.status),
    bytes: String(new TextEncoder().encode(text).length),
    text,
    fetchedAt: now(),
  };

  if (!received.ok) {
    const { error, message } = errorBody(text, route);
    return { state: response.status === NOT_FOUND ? "absent" : "error", response, error, message };
  }

  try {
    return { state: "ok", response, wire: parseWire(text, route) };
  } catch (thrown) {
    if (thrown instanceof WireUnsupported) {
      return { state: "error", response, error: thrown.name, message: thrown.message };
    }
    // A 2xx whose body is not JSON - an intermediary's page, a truncated response - is the same
    // unreadable body `errorBody` already names, and it is a state, never a rejection:
    // `PanelFrame` renders states, so a rethrow here would leave the panel loading for ever.
    return { state: "error", response, error: UNREADABLE, message: text };
  }
}
