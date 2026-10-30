"use client";

/**
 * One session's episodes, in the order the session wrote them (FR-58, UJ-10, `D-3`).
 *
 * This is the route decision 24 wrote two sentences for, and the panel is why they are two. The
 * gateway looks the session up in `events` first, so a 404 means the events table never held this
 * session id - the operator pasted the wrong kind of id - while a 200 with zero rows means the
 * session ran and wrote no episode. Both sentences live in `lib/null-reasons.ts` and `PanelFrame`
 * picks whichever state the response actually is; nothing here restates either of them, and
 * nothing here can say one where the other is true.
 *
 * `run_id` is printed under its own field name rather than translated. The ledger's column is what
 * an operator greps for, and for this table it holds the session id the episode was written under
 * (the gateway route table).
 */
import { DataTable } from "../../components/DataTable";
import { PanelFrame } from "../../components/PanelFrame";
import { Value } from "../../components/Value";
import type { Wire } from "../../lib/wire";

import { rowsOf } from "../../lib/rows";
import { RUN_COMMAND, episodesRoute } from "./search";
import styles from "./memory.module.css";

/** The panel's name, so a test and a screen reader find it by the same words. */
export const PANEL_TITLE = "Episodes";

type Props = {
  /** The session id the URL carried, passed to the gateway exactly as it arrived. */
  sessionId: string;
};

export function EpisodesPanel({ sessionId }: Props) {
  const route = episodesRoute(sessionId);

  return (
    <PanelFrame route={route} title={PANEL_TITLE} sourceLabel="Ledger: this session" cliTwin={RUN_COMMAND}>
      {(wire) => {
        const rows = rowsOf(wire, (index) => `.episodes[${index}].episode_id`);
        if (rows.length === 0) return null;
        return (
          <div className={styles.body}>
            <DataTable
              caption="Episodes, in the order the gateway sent them"
              rows={rows}
              columns={[
                {
                  header: "episode_id",
                  cell: (row) => <Value leaf={at(wire, row, "episode_id")} />,
                },
                { header: "kind", cell: (row) => <Value leaf={at(wire, row, "kind")} /> },
                { header: "run_id", cell: (row) => <Value leaf={at(wire, row, "run_id")} /> },
                {
                  header: "text",
                  cell: (row) => (
                    <span className={styles.episodeText}>
                      <Value leaf={at(wire, row, "text")} />
                    </span>
                  ),
                },
                { header: "created", cell: (row) => <Value leaf={at(wire, row, "created_at")} /> },
              ]}
            />
          </div>
        );
      }}
    </PanelFrame>
  );
}

/** One field of one episode row, as a leaf of this panel's own response. */
function at(wire: Wire, row: number, field: string) {
  return wire.at(`.episodes[${row}].${field}`);
}
