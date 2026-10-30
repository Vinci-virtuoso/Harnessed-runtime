"use client";

/**
 * `/improve`, the promotion history: every decision the promoter recorded, in decided order
 * (FR-57, UJ-8).
 *
 * One panel, one request, one table. The rows are the response's rows and their order is the
 * response's order - the store returns them in decided order and there is no sort control to offer
 * (`D-1`, MUST rule 21).
 *
 * **`promoted` is a `PassMark` wearing its own field name.** Only `passed` is a verdict of pass or
 * fail; `promoted: false` means the promoter declined, and calling that a "fail" would invent a
 * judgement the ledger never made. `PassMark` prints the verbatim field for every boolean but
 * `passed`, which is exactly this case.
 *
 * **A row links nowhere.** A promotion carries a `cand_id` and no improvement run id, no suite and
 * no procedure link (`[GAP G-12]`), so there is no id here to build a link out of and this page
 * does not invent one by splitting or joining what it was sent (MUST rule 25).
 *
 * The two scores are one `ScorePair`: two read figures with a plain `->` between them and no
 * difference anywhere, because deciding whether a move is an improvement is `improve/promoter.py`'s
 * and arrives here as `reason` (MUST rules 1 and 21).
 */
import { DataTable } from "../../components/DataTable";
import { PanelFrame } from "../../components/PanelFrame";
import { PassMark } from "../../components/PassMark";
import { ScorePair } from "../../components/ScorePair";
import { Value } from "../../components/Value";
import { now } from "../../lib/clock";
import { humanAgo } from "../../lib/humanize";
import type { Wire } from "../../lib/wire";

import { rowsOf } from "../../lib/rows";
import { RUN_COMMAND, promotionsFooterLine, promotionsRoute } from "./limit";
import styles from "./improve.module.css";

/** The panel's name, so a test and a screen reader find it by the same words. */
export const PANEL_TITLE = "Promotions";

type Props = {
  /** The limit the URL is carrying, as its characters. */
  limit: string;
};

export function PromotionsPanel({ limit }: Props) {
  const route = promotionsRoute(limit);

  return (
    <PanelFrame route={route} title={PANEL_TITLE} sourceLabel="Ledger: all time" cliTwin={RUN_COMMAND}>
      {(wire) => {
        const rows = rowsOf(wire, (index) => `.promotions[${index}].cand_id`);
        if (rows.length === 0) return null;
        return (
          <div className={styles.body}>
            <DataTable
              caption="Promotion decisions, in the order the gateway sent them"
              rows={rows}
              columns={[
                {
                  header: "candidate id",
                  cell: (row) => <Value leaf={at(wire, row, "cand_id")} />,
                },
                {
                  header: "decided",
                  cell: (row) => {
                    const decided = at(wire, row, "decided_at");
                    return (
                      <span className={styles.nowrap}>
                        <Value
                          leaf={decided}
                          human={decided.kind === "null" ? undefined : humanAgo(decided, now())}
                        />
                      </span>
                    );
                  },
                },
                { header: "promoted", cell: (row) => <PassMark leaf={at(wire, row, "promoted")} /> },
                { header: "reason", cell: (row) => <Value leaf={at(wire, row, "reason")} /> },
                {
                  header: "score",
                  cell: (row) => (
                    <ScorePair
                      before={at(wire, row, "score_before")}
                      after={at(wire, row, "score_after")}
                    />
                  ),
                },
                {
                  header: "passes before",
                  cell: (row) => <Value leaf={at(wire, row, "passes_before")} />,
                },
                {
                  header: "passes after",
                  cell: (row) => <Value leaf={at(wire, row, "passes_after")} />,
                },
                {
                  header: "regressions",
                  cell: (row) => <Value leaf={at(wire, row, "regressions")} />,
                },
                {
                  header: "soft drops",
                  cell: (row) => <Value leaf={at(wire, row, "soft_drops")} />,
                },
              ]}
            />
            <p className={styles.footer} data-testid="promotions-footer">
              {promotionsFooterLine(limit)}
            </p>
          </div>
        );
      }}
    </PanelFrame>
  );
}

/** One field of one promotion row, as a leaf of this panel's own response. */
function at(wire: Wire, row: number, field: string) {
  return wire.at(`.promotions[${row}].${field}`);
}
