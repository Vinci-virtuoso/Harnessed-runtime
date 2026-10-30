"use client";

/**
 * `/traces`, the Sessions listing: every session the events table holds, in the gateway's order
 * (FR-48, FR-59, UJ-11).
 *
 * One panel, one request, one table. The rows are the response's rows and their order is the
 * response's order - there is no sort control to offer and nothing here reorders anything (`D-1`,
 * MUST rule 21). Each row's link is its own `session_id`, taken verbatim from the row that
 * carries it: a session id is never split, trimmed or reformatted on the way into a URL (MUST
 * rules 15, 25).
 *
 * **A session with no terminal event reads `unknown`, not "running".** FR-48 sends
 * `terminal_event: null` for a session whose finish has not landed, and that covers a run still
 * in flight *and* a publisher that crashed - two different facts the gateway does not tell apart.
 * Printing "live" would be this page deciding between them, so the absence is rendered as the
 * absence (`D-2`) and the session's own page is where the stream says which it is.
 *
 * The one `?limit=` control on `/traces` sits in the Runs section; the same `?limit=` in the URL
 * drives this listing too, so there is one control and not two that could disagree (`D-6`).
 */
import { DataTable } from "../../components/DataTable";
import { PanelFrame } from "../../components/PanelFrame";
import { Value } from "../../components/Value";
import { nullReasonFor } from "../../lib/null-reasons";
import { rowsOf } from "../../lib/rows";
import type { Leaf } from "../../lib/wire";

import { RUN_COMMAND, footerLine, listingRoute, sessionHref } from "./limit";
import styles from "./sessions.module.css";

/** The panel's own name, exported so a test names the region rather than guessing at it. */
export const PANEL_TITLE = "Sessions";

/** A consolidation batch runs as a session of its one event: it is worded, not shown as a name. */
const CONSOLIDATED_EVENT = "memory.consolidation.finished";
export const CONSOLIDATED_WORDS = "Memory consolidated";

function EventName({ leaf, nullReason }: { leaf: Leaf; nullReason?: string }) {
  const human = leaf.text === CONSOLIDATED_EVENT ? { text: CONSOLIDATED_WORDS, exact: leaf.text } : undefined;
  return <Value tabIndex={-1} leaf={leaf} human={human} nullReason={nullReason} />;
}

type Props = {
  /** The limit the URL is carrying, as its characters. */
  limit: string;
};

export function SessionsPanel({ limit }: Props) {
  const route = listingRoute(limit);

  return (
    <PanelFrame route={route} title={PANEL_TITLE} sourceLabel="Ledger: all time" cliTwin={RUN_COMMAND}>
      {(wire) => {
        const rows = rowsOf(wire, (index) => `.sessions[${index}].session_id`);
        if (rows.length === 0) return null;
        return (
          <div className={styles.body}>
            <DataTable
              caption="Sessions, in the order the gateway sent them"
              rows={rows}
              rowLink={{
                header: "session id",
                leaf: (row) => wire.at(`.sessions[${row}].session_id`),
                href: (row) => sessionHref(wire.at(`.sessions[${row}].session_id`).text),
              }}
              columns={[
                {
                  header: "first event",
                  cell: (row) => <EventName leaf={wire.at(`.sessions[${row}].first_event`)} />,
                },
                {
                  header: "first ts",
                  cell: (row) => <Value tabIndex={-1} leaf={wire.at(`.sessions[${row}].first_ts`)} />,
                },
                {
                  header: "terminal event",
                  cell: (row) => (
                    <EventName
                      leaf={wire.at(`.sessions[${row}].terminal_event`)}
                      nullReason={nullReasonFor(wire.route, ".sessions[].terminal_event")}
                    />
                  ),
                },
              ]}
            />
            <p className={styles.footer} data-testid="sessions-footer">
              {footerLine(limit)}
            </p>
          </div>
        );
      }}
    </PanelFrame>
  );
}
