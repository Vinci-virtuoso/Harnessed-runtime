"use client";

/**
 * `/memory/facts?q=`, what a search matched (FR-58, UJ-10).
 *
 * One panel, one request, one table, and the request only happens once the URL carries a query.
 * The rows are the response's rows in the response's order - the search ranked them, best match
 * first, and there is no sort control to offer (`D-1`, MUST rule 21).
 *
 * **Zero hits name the query, not the store.** `/v1/memory/facts` answers `[]` both for a query
 * that matched nothing and for no query at all, so a count here would report an empty store where
 * nothing was ever asked (`[GAP G-14]`). The empty state therefore says "no active fact matches
 * <q>", with the query the URL carried, and the page never renders "0 facts".
 *
 * **A row links by its own `mem_id`.** The id is the leaf the response sent and the href is built
 * from that same leaf, so the link is the id and never a reformatting of one (MUST rule 25). That
 * link is how an archived fact is reached too, because the search serves the active set and a
 * fact's own page serves it archived or not.
 */
import { DataTable } from "../../components/DataTable";
import { PanelFrame } from "../../components/PanelFrame";
import { Value } from "../../components/Value";
import { now } from "../../lib/clock";
import { humanAgo, humanFromConsolidation } from "../../lib/humanize";
import type { Wire } from "../../lib/wire";

import { rowsOf } from "../../lib/rows";
import { ADD_COMMAND, factHref, factsRoute, noMatch } from "./search";
import styles from "./memory.module.css";

/** The panel's name, so a test and a screen reader find it by the same words. */
export const PANEL_TITLE = "Facts";

/** Whose order the rows are in, said where an operator would otherwise have to assume. */
export const ORDER_NOTE = "in the order the gateway ranked them: best match first";

type Props = {
  /** The query the URL is carrying, as its characters. */
  q: string;
};

export function FactsPanel({ q }: Props) {
  const route = factsRoute(q);

  return (
    <PanelFrame
      route={route}
      title={PANEL_TITLE}
      sourceLabel="Ledger: all time"
      emptyText={noMatch(q)}
      cliTwin={ADD_COMMAND}
    >
      {(wire) => {
        const rows = rowsOf(wire, (index) => `.facts[${index}].mem_id`);
        if (rows.length === 0) return null;
        return (
          <div className={styles.body}>
            <DataTable
              caption="Facts that matched, in the order the gateway sent them"
              rows={rows}
              rowLink={{
                header: "mem_id",
                leaf: (row) => at(wire, row, "mem_id"),
                href: (row) => factHref(at(wire, row, "mem_id").text),
              }}
              columns={[
                { header: "text", cell: (row) => <Value leaf={at(wire, row, "text")} /> },
                {
                  header: "source",
                  cell: (row) => {
                    const source = at(wire, row, "source");
                    const ago = humanAgo(at(wire, row, "created_at"), now());
                    return <Value leaf={source} human={humanFromConsolidation(source, ago)} />;
                  },
                },
                { header: "status", cell: (row) => <Value leaf={at(wire, row, "status")} /> },
                { header: "created", cell: (row) => <Value leaf={at(wire, row, "created_at")} /> },
              ]}
            />
            <p className={styles.note} data-testid="facts-note">
              {ORDER_NOTE}
            </p>
          </div>
        );
      }}
    </PanelFrame>
  );
}

/** One field of one fact row, as a leaf of this panel's own response. */
function at(wire: Wire, row: number, field: string) {
  return wire.at(`.facts[${row}].${field}`);
}
