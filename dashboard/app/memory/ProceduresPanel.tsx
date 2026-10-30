"use client";

/**
 * `/memory/procedures`, the active set: the procedures that actually reach a system prompt
 * (FR-58, UJ-10).
 *
 * One panel, one request, one table. The rows are the response's rows and their order is the
 * response's order - `memory/procedural.py::active` returns the active set and there is no sort
 * control to offer (`D-1`, MUST rule 21).
 *
 * **A row links nowhere.** A procedure carries a `proc_id` and a `source` that names the promotion
 * it came from in prose, and there is no promotion id in it to link with and no procedure history
 * to link to (`[GAP G-16]`), so this page builds no link out of a sentence (MUST rule 25).
 *
 * The note under the table is the other half of that gap said out loud: this is the active set and
 * not every procedure ever written, which is a fact about what was asked for rather than about
 * what the store holds.
 */
import { DataTable } from "../../components/DataTable";
import { PanelFrame } from "../../components/PanelFrame";
import { Value } from "../../components/Value";
import type { Wire } from "../../lib/wire";

import { rowsOf } from "../../lib/rows";
import { PROCEDURES_ROUTE, PROMOTE_COMMAND } from "./search";
import styles from "./memory.module.css";

/** The panel's name, so a test and a screen reader find it by the same words. */
export const PANEL_TITLE = "Active procedures";

/** What this table is, and is not: the active set, never a history (`[GAP G-16]`). */
export const ACTIVE_ONLY =
  "the active set, as the gateway serves it: a retired procedure is in no row here";

export function ProceduresPanel() {
  return (
    <PanelFrame
      route={PROCEDURES_ROUTE}
      title={PANEL_TITLE}
      sourceLabel="Ledger: all time"
      cliTwin={PROMOTE_COMMAND}
    >
      {(wire) => {
        const rows = rowsOf(wire, (index) => `.procedures[${index}].proc_id`);
        if (rows.length === 0) return null;
        return (
          <div className={styles.body}>
            <DataTable
              caption="Active procedures, in the order the gateway sent them"
              rows={rows}
              columns={[
                { header: "proc_id", cell: (row) => <Value leaf={at(wire, row, "proc_id")} /> },
                { header: "text", cell: (row) => <Value leaf={at(wire, row, "text")} /> },
                { header: "source", cell: (row) => <Value leaf={at(wire, row, "source")} /> },
                {
                  header: "created",
                  cell: (row) => <Value leaf={at(wire, row, "created_at")} />,
                },
              ]}
            />
            <p className={styles.note} data-testid="procedures-note">
              {ACTIVE_ONLY}
            </p>
          </div>
        );
      }}
    </PanelFrame>
  );
}

/** One field of one procedure row, as a leaf of this panel's own response. */
function at(wire: Wire, row: number, field: string) {
  return wire.at(`.procedures[${row}].${field}`);
}
