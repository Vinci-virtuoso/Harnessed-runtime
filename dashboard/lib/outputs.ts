/**
 * The output body URL (FR-127, decision 65).
 *
 * `GET /v1/outputs/{output_id}` answers the stored bytes of one delegation output with their own
 * media type - decision 38's second named exception to canonical JSON (the SSE stream is the
 * first) - so this module holds no `parseWire` read, only the URL. It is one of the four modules
 * the endpoint scan will exempt (`lib/fetch-panel.ts`, `lib/stream.ts`, `lib/control-client.ts`,
 * this one, decision 68): `ImageView` and `DiagramView` hand the URL straight to an `<img src>`,
 * and `UiPreview` fetches it itself as text, because `D-10` forbids an `iframe` `src`.
 */

/** The route template `lib/cli-twins.ts` already keys its twin on. */
export const OUTPUTS_ROUTE = "/v1/outputs";

/** The one output's body, at the relative path on the origin that served the page. */
export function outputBodyUrl(outputId: string): string {
  return `${OUTPUTS_ROUTE}/${encodeURIComponent(outputId)}`;
}
