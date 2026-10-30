"use client";

/**
 * The Overview's `TasksTile`: `GET /v1/tasks/totals`, one read, one panel (FR-153, `D-1`). The
 * headline is `completed_unattended`; beside it `running`, `queued` and `needs_human`, each the
 * served figure and never a sum, rate or "of N" (G-48). "Needs you" is `color-warn` only when the
 * served figure is not `"0"`. The gloss is the tile's own definition of the headline.
 */
import type { Leaf } from "../lib/wire";

import { PanelFrame } from "./PanelFrame";
import { StatTile } from "./StatTile";
import styles from "./TasksTile.module.css";

export const TASKS_TITLE = "Tasks";
export const TASKS_ROUTE = "/v1/tasks/totals";
export const COMPLETED_GLOSS = "Done with no message from you and no pause. All time.";

const NOT_COUNTED = "the gateway did not report this count";

function reasonOf(leaf: Leaf): string | undefined {
  return leaf.kind === "null" ? NOT_COUNTED : undefined;
}

export function TasksTile({ refresh }: { refresh?: object }) {
  return (
    <PanelFrame route={TASKS_ROUTE} title={TASKS_TITLE} sourceLabel="Tasks: all time" refresh={refresh}>
      {(wire) => {
        const completed = wire.at(".completed_unattended");
        const running = wire.at(".running");
        const queued = wire.at(".queued");
        const needs = wire.at(".needs_human");
        return (
          <>
            <div className={styles.tiles}>
              <div className={styles.headline}>
                <StatTile label="Completed unattended" leaf={completed} nullReason={reasonOf(completed)} />
              </div>
              <div className={styles.small}>
                <StatTile label="Running" leaf={running} nullReason={reasonOf(running)} />
                <StatTile label="Queued" leaf={queued} nullReason={reasonOf(queued)} />
                <div className={needs.text === "0" ? undefined : styles.warn}>
                  <StatTile label="Needs you" leaf={needs} nullReason={reasonOf(needs)} />
                </div>
              </div>
            </div>
            <p className={styles.gloss}>{COMPLETED_GLOSS}</p>
          </>
        );
      }}
    </PanelFrame>
  );
}
