/**
 * One aggregate route's group: a card of its figures, then the window they count over (decision
 * 47). Shared by the Overview and the Memory page's figures, so both render a count identically.
 *
 * This is the `StatGroup` of design.md (v6 amendment, FR-163): a `PanelFrame` in its card look, so
 * a group is still one route, one fetch and one freshness. It draws a title, a row of headline
 * `StatTile`s (`Headlines`), a row of secondary ones (`Secondary`), then any gloss; the loading
 * state is the title and one skeleton bar per headline.
 */
import type { ReactNode } from "react";

import type { Wire } from "../lib/wire";
import { PanelFrame } from "./PanelFrame";
import styles from "./Aggregate.module.css";

/** The window every aggregate counts over (decision 47): no `?since=`, no cache. */
export const WINDOW = "all time, counted now";

type Props = {
  route: string;
  title: string;
  refresh: object | undefined;
  /** How many headline figures the group draws: the loading state shows one bar for each. */
  headlines: number;
  children: (wire: Wire) => ReactNode;
};

export function Aggregate({ route, title, refresh, headlines, children }: Props) {
  return (
    <PanelFrame
      route={route}
      title={title}
      sourceLabel="Ledger: all time"
      refresh={refresh}
      skeletonRows={headlines}
      card
    >
      {(wire) => (
        <div className={styles.body}>
          {children(wire)}
          <p className={styles.window}>{WINDOW}</p>
        </div>
      )}
    </PanelFrame>
  );
}

/** The group's headline figures, in a wrapping row with a 24px column gap. */
export function Headlines({ children }: { children: ReactNode }) {
  return <div className={styles.headlines}>{children}</div>;
}

/** The group's secondary figures, in a second wrapping row. */
export function Secondary({ children }: { children: ReactNode }) {
  return <div className={styles.secondary}>{children}</div>;
}
