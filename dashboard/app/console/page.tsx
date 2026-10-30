"use client";

/**
 * `/console` - start a run from one prompt and watch it through the stream that already exists
 * (FR-81, UJ-16; design.md "The operator console"), and `/console?session=` one session opened
 * again from `HISTORY` (FR-83, decision 50).
 *
 * A client component holding no data (decision 34): it reads the query and nothing else, and every
 * figure below it is fetched or answered in the browser. `?session=` is the id a `HISTORY` row
 * linked with, passed to the panel verbatim (MUST rule 25); the panel replays it, streams it from
 * the replayed cursor and continues it on the next `Send`. A reload of the same URL is the same
 * page, because the ledger is the state (`D-6`) and the browser keeps nothing between the two.
 *
 * The panel is keyed on the session, so opening another one starts from its own replay rather than
 * from the state of the one before it.
 */
import { useSearchParams } from "next/navigation";

import { ConsolePanel } from "../../components/ConsolePanel";
import { HistoryPanel, limitLeaf } from "../../components/HistoryPanel";
import { DEFAULT_LIMIT } from "../traces/limit";

import styles from "./console.module.css";

export default function ConsolePage() {
  // Read here, in the browser, because an export has no server to read it on (decision 45).
  const query = useSearchParams();
  const session = query.get("session");
  const limit = query.get("limit") ?? DEFAULT_LIMIT;

  return (
    <div className={styles.page}>
      <h1 className={styles.title}>Console</h1>
      <p className={styles.subtitle}>
        Send one prompt to the harness and watch each step of the run it opens until its reply
        lands.
      </p>
      <HistoryPanel limit={limitLeaf(limit)} />
      <ConsolePanel key={session ?? ""} sessionId={session} />
    </div>
  );
}
