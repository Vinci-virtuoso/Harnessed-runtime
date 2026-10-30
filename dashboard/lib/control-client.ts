/**
 * The one door that writes (decisions 43, 49; MUST rules 22, 26).
 *
 * Every write the dashboard ever makes goes through here, and here is the only module under
 * `app/`, `components/` and `lib/` allowed a request method other than GET
 * (`tests/test_boundaries.py::test_dashboard_reads_only_the_gateway`). It reaches the control app
 * at the relative `/control/v1/...` on the origin that served the page, and nothing else: never
 * the read gateway, whose v1 surface stays GET-only, and never a second origin. Every route this
 * module names starts with the control prefix, and the scan fails on one that does not.
 *
 * It is a thin client with no caller yet. Ticket 137 gives the console its `Send`, ticket 145 the
 * Models page its credential form; both call these three and nothing else. The body of a run is
 * **one string** under the key `prompt` (MUST rule 26): the runtime carries the session's
 * messages, the browser never assembles a history.
 *
 * Like `fetch-panel.ts`, a rejected `fetch` is the one unreachable signal (MUST rule 23); every
 * non-2xx is the control app's own `{"error","message"}` body, quoted verbatim. The 202 of a run
 * and the 200 of a credential are read through `parseWire`, so a session id is the characters
 * the wire spelled (MUST rule 25).
 */
import { now } from "./clock";
import { WireUnsupported, parseWire, type Wire } from "./wire";

/** The route table of decision 49, spelled once, at the relative path on this page's origin. */
export const RUNS_ROUTE = "/control/v1/runs";
export const PROVIDERS_ROUTE = "/control/v1/providers";
export const TASKS_ROUTE = "/control/v1/tasks";

/** The one method this module sends, named for the chip of a figure read out of its answer. */
export const CONTROL_METHOD = "POST";

/** The messages route for one session, its id placed verbatim (MUST rule 25). */
export function messagesRoute(sessionId: string): string {
  return `/control/v1/runs/${encodeURIComponent(sessionId)}/messages`;
}

/** The permission answer route for one request, its id placed verbatim (MUST rule 25). */
export function permissionRoute(permissionId: string): string {
  return `/control/v1/permissions/${encodeURIComponent(permissionId)}`;
}

/** A body that is not decision 24's error contract is quoted as itself under this class name. */
const UNREADABLE = "UnreadableBody";

/** The response as it arrived, whole, for the same drawer a read shows (FR-51). */
export type ControlResponse = {
  /** The request's own URL: `/control/v1/...`. */
  url: string;
  /** The HTTP status as text (decision 32). */
  status: string;
  /** The body's UTF-8 length, as text. */
  bytes: string;
  /** The raw body, exactly as received. */
  text: string;
  /** When the browser received it, on the dashboard's clock (`D-5`). */
  fetchedAt: Date;
};

/** Exactly one state, as `PanelResult` has: accepted, refused by the control app, or no answer. */
export type ControlResult =
  | { state: "ok"; response: ControlResponse; wire: Wire }
  | { state: "error"; response: ControlResponse; error: string; message: string }
  | { state: "unreachable"; url: string; cause: string };

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
 * One `POST` to the control app, with a JSON body, turned into exactly one state.
 *
 * @param route a `/control/v1/...` route from this module's table
 * @param body the JSON object the route takes; a run's is `{ prompt }` and nothing else
 * @param signal aborts the request when the caller unmounts
 */
export async function postControl(
  route: string,
  body: Record<string, string>,
  signal?: AbortSignal,
): Promise<ControlResult> {
  let received: Response;
  let text: string;
  try {
    received = await fetch(route, {
      method: "POST",
      cache: "no-store",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
      signal,
    });
    text = await received.text();
  } catch (thrown) {
    return { state: "unreachable", url: route, cause: causeOf(thrown) };
  }
  const response: ControlResponse = {
    url: route,
    status: String(received.status),
    bytes: String(new TextEncoder().encode(text).length),
    text,
    fetchedAt: now(),
  };
  if (!received.ok) {
    const { error, message } = errorBody(text, route);
    return { state: "error", response, error, message };
  }
  try {
    return { state: "ok", response, wire: parseWire(text, route) };
  } catch (thrown) {
    if (thrown instanceof WireUnsupported) {
      return { state: "error", response, error: thrown.name, message: thrown.message };
    }
    return { state: "error", response, error: UNREADABLE, message: text };
  }
}

/** Start a run from one prompt: `POST /control/v1/runs`, `202 {"session_id","stream","run"}`. */
export function startRun(prompt: string, signal?: AbortSignal): Promise<ControlResult> {
  return postControl(RUNS_ROUTE, { prompt }, signal);
}

/** Continue a session with one more prompt: the same 202, the same session id. */
export function sendMessage(
  sessionId: string,
  prompt: string,
  signal?: AbortSignal,
): Promise<ControlResult> {
  return postControl(messagesRoute(sessionId), { prompt }, signal);
}

/** Configure a provider from a credential: `200`, `init --json`'s payload, never the key back. */
export function configureProvider(
  provider: string,
  credential: string,
  signal?: AbortSignal,
): Promise<ControlResult> {
  return postControl(PROVIDERS_ROUTE, { provider, credential }, signal);
}

/** The two answers an operator gives an agent's request (`acp/pending.py::ANSWERS`). */
export type PermissionAnswer = "approve" | "deny";

/** Answer one open permission request: `POST /control/v1/permissions/{id}`, `{"answer"}`, once. */
export function answerPermission(
  permissionId: string,
  answer: PermissionAnswer,
  signal?: AbortSignal,
): Promise<ControlResult> {
  return postControl(permissionRoute(permissionId), { answer }, signal);
}

/** Queue one string as a task: `POST /control/v1/tasks`, `201` with the budget it got. Never runs it. */
export function queueTask(text: string, signal?: AbortSignal): Promise<ControlResult> {
  return postControl(TASKS_ROUTE, { text }, signal);
}
