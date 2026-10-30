"use client";

/**
 * The one field on `/improve` an operator types into (FR-60, FR-57; `[GAP G-8]`).
 *
 * There is no listing of improvement cycles - the gateway serves one by id and by nothing else -
 * so a cycle is reached by pasting the id `harnessed improve run --json` printed. `IdLookup` sends
 * what was typed exactly as typed and never asks whether it exists; the 404 belongs to the cycle
 * page, where it arrives as `AbsentState` quoting the gateway's own message.
 *
 * It is a client component because `hrefFor` is a function, and a function cannot cross the server
 * boundary. That is the whole of this file's reason to exist.
 */
import { IdLookup } from "../../components/IdLookup";

import { cycleHref } from "./limit";

export function CycleLookup() {
  return <IdLookup kind="improvement run id" hrefFor={cycleHref} />;
}
