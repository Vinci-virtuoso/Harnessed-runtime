"use client";

/**
 * `/traces`, the Runs listing: every loop run the journal holds, in the gateway's order (FR-55, UJ-7).
 *
 * One panel, one request, one table. The rows are the response's rows and their order is the
 * response's order - there is no sort control to offer and nothing here reorders anything (`D-1`,
 * MUST rule 21). Each row's link is its own `run_key`, taken verbatim from the row that carries
 * it: a case run key holds two `:` separators and is never split to make a prettier link
 * (MUST rules 15, 25).
 *
 * The `?limit=` control is the one thing on this page the client owns, and it owns it in the URL:
 * choosing 50 is a navigation, so the page that renders next is the page for that URL, and the
 * dashboard remembers nothing between the two (`D-6`). The footer says "up to" because the limit
 * is a ceiling the operator asked for, not a count of what arrived; counting the rows would be
 * this page deriving a figure the gateway never sent.
 */
import { useRouter } from "next/navigation";

import { DataTable } from "../../components/DataTable";
import { KeyKindTag } from "../../components/KeyKindTag";
import { PanelFrame } from "../../components/PanelFrame";
import { StopReasonTag } from "../../components/StopReasonTag";
import { Value } from "../../components/Value";
import { nullReasonFor } from "../../lib/null-reasons";

import { LIMITS, footerLine, listingHref, runHref, runsListingRoute } from "./limit";
import { rowsOf } from "../../lib/rows";
import styles from "./runs.module.css";

/** What an operator runs to put a row here, where the journal is empty (`EmptyState`, `D-3`). */
const CLI_TWIN = "harnessed run";

type Props = {
  /** The limit the URL is carrying, as its characters. */
  limit: string;
};

export function RunsPanel({ limit }: Props) {
  const route = runsListingRoute(limit);

  return (
    <PanelFrame route={route} title="Runs" sourceLabel="Ledger: all time" cliTwin={CLI_TWIN}>
      {(wire) => {
        const rows = rowsOf(wire, (index) => `.runs[${index}].run_key`);
        if (rows.length === 0) return null;
        return (
          <div className={styles.body}>
            <LimitControl limit={limit} />
            <DataTable
              caption="Loop runs, in the order the gateway sent them"
              rows={rows}
              rowLink={{
                header: "run key",
                leaf: (row) => wire.at(`.runs[${row}].run_key`),
                href: (row) => runHref(wire.at(`.runs[${row}].run_key`).text),
              }}
              columns={[
                {
                  header: "kind",
                  cell: (row) => <KeyKindTag tabIndex={-1} leaf={wire.at(`.runs[${row}].key_kind`)} />,
                },
                {
                  header: "started",
                  cell: (row) => <Value tabIndex={-1} leaf={wire.at(`.runs[${row}].started_at`)} />,
                },
                {
                  header: "iterations",
                  cell: (row) => <Value tabIndex={-1} leaf={wire.at(`.runs[${row}].iterations`)} />,
                },
                {
                  header: "stop reason",
                  cell: (row) => (
                    <StopReasonTag
                      tabIndex={-1}
                      leaf={wire.at(`.runs[${row}].stop_reason`)}
                      nullReason={nullReasonFor(wire.route, ".runs[].stop_reason")}
                    />
                  ),
                },
              ]}
            />
            <p className={styles.footer} data-testid="runs-footer">
              {footerLine(limit)}
            </p>
          </div>
        );
      }}
    </PanelFrame>
  );
}

/**
 * The 20 / 50 / 100 choice, as a radio group, because it is one choice out of a closed set and a
 * radio says so to a keyboard and a screen reader without anything being scripted. Choosing
 * pushes the URL; the URL is read back by the page, which is what makes the choice survive a
 * reload and a shared link without a single remembered byte.
 */
function LimitControl({ limit }: Props) {
  const router = useRouter();

  return (
    <fieldset className={styles.limit} data-testid="limit-control">
      <legend className={styles.limitLegend}>rows to ask for</legend>
      {LIMITS.map((choice) => (
        <label key={choice} className={styles.limitChoice}>
          <input
            type="radio"
            name="limit"
            value={choice}
            checked={choice === limit}
            onChange={() => router.push(listingHref(choice))}
          />
          {choice}
        </label>
      ))}
    </fieldset>
  );
}
