"use client";

/**
 * Whether the gateway answers, in the words decision 39 fixes (FR-54; design.md inventory).
 *
 * **It says "gateway reachable", never "healthy".** `/health` answers without opening the
 * database - that is why UJ-12's Path C exists, where the gateway is up and every data route is a
 * 500 `StorageError` - so the only claim this pill is entitled to make is that something answered
 * at the URL it names. The reading of the ledger belongs to the panels, and a panel that fails
 * says so for itself.
 *
 * It is **the one component that polls** (decision 34): `/health` every 15 seconds, through the
 * same `lib/fetch-panel.ts` every other read goes through, so the unreachable header is read here
 * exactly as it is read in a panel (MUST rule 23). Data routes are read on navigation and on
 * `Refetch` and at no other time (`D-6`).
 *
 * The announcement is a live region holding the state's sentence and nothing else, so a screen
 * reader hears the change and only the change: the sentence is a function of the state, so a poll
 * that finds the same state rewrites nothing. The visible pill sits outside that region, because a
 * URL that never changes has no business being re-read every fifteen seconds.
 *
 * Clicking it opens the response it is speaking for, in the same `ProvenanceDrawer` a figure
 * opens. While there is nothing to show - before the first answer, or when the gateway never
 * answered at all - the pill is disabled rather than opening an empty drawer.
 */
import { useContext, useEffect, useState } from "react";

import { fetchPanel, type PanelResult, type RawResponse } from "../lib/fetch-panel";
import type { Leaf } from "../lib/wire";

import { GatewayOriginContext } from "./PanelFrame";
import { ProvenanceDrawer } from "./ProvenanceDrawer";
import styles from "./GatewayStatusPill.module.css";

/** The pill speaks for the whole answer rather than one field of it (`D-9`): a leaf with no path. */
const WHOLE_ANSWER: Leaf = { kind: "string", text: "", path: "", route: "" };

const HARNESS_SOURCE = "Harness: this machine";

/** The one route this component reads, as the route table spells it. */
export const HEALTH_ROUTE = "/v1/health";

/** Every 15 s, as `ux/design.md` and decision 34 both state it. A literal: this tree does no math. */
export const POLL_MS = 15000;

/** The four states `design.md` names for this pill, and nothing else. */
export type PillState = "checking" | "reachable" | "unreachable" | "error";

/** One answer and the gateway it answered for: a reading can never outlive the read (`D-6`). */
type Answered = { origin: string; result: PanelResult };

export function GatewayStatusPill() {
  const origin = useContext(GatewayOriginContext);
  const [answered, setAnswered] = useState<Answered | undefined>(undefined);
  const [open, setOpen] = useState(false);

  const result = answered !== undefined && answered.origin === origin ? answered.result : undefined;

  useEffect(() => {
    const controller = new AbortController();
    let live = true;
    const read = (): void => {
      void fetchPanel(HEALTH_ROUTE, origin, controller.signal).then((next) => {
        if (live) setAnswered({ origin, result: next });
      });
    };
    read();
    const poll = setInterval(read, POLL_MS);
    return () => {
      live = false;
      clearInterval(poll);
      controller.abort();
    };
  }, [origin]);

  const state = stateOf(result);
  const response = keptResponse(result);
  const label = labelOf(state, response);

  return (
    <>
      <div className={styles.live} role="status" aria-live="polite">
        <span className={styles.announcement} data-testid="pill-announcement">
          {state === "checking" ? "checking the gateway" : label}
        </span>
      </div>
      <button
        type="button"
        id="gateway-status-pill"
        className={styles.pill}
        data-state={state}
        data-testid="gateway-status-pill"
        aria-haspopup="dialog"
        aria-expanded={open}
        disabled={response === undefined}
        onClick={() => setOpen(true)}
      >
        <span className={styles.dot} data-state={state} aria-hidden="true" />
        <span className={styles.label}>{label}</span>
      </button>
      {open && response !== undefined ? (
        <ProvenanceDrawer
          response={response}
          sourceLabel={HARNESS_SOURCE}
          leaf={WHOLE_ANSWER}
          onClose={() => {
            setOpen(false);
            document.getElementById("gateway-status-pill")?.focus();
          }}
        />
      ) : null}
    </>
  );
}

/** The state an answer puts the pill in. A non-2xx is an error; only the header is unreachable. */
function stateOf(result: PanelResult | undefined): PillState {
  if (result === undefined) return "checking";
  if (result.state === "unreachable") return "unreachable";
  if (result.state === "ok") return "reachable";
  return "error";
}

/** The response the drawer would show, where the gateway sent one at all. */
function keptResponse(result: PanelResult | undefined): RawResponse | undefined {
  if (result === undefined || result.state === "unreachable") return undefined;
  return result.response;
}

/** What the pill reads. The status is the gateway's own characters, never a word for them. */
function labelOf(state: PillState, response: RawResponse | undefined): string {
  if (state === "checking") return "checking";
  if (state === "reachable") return "gateway reachable";
  if (state === "unreachable") return "gateway unreachable";
  return `gateway error ${response === undefined ? "" : response.status}`;
}
