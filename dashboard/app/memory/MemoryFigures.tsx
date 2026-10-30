"use client";

/**
 * The Memory page's figures row: the gate split and the memory totals, each a `PanelFrame` of its
 * own aggregate route, rendered as the Overview renders them (decision 42, `D-1`): raw counts in
 * the declared order, no sum and no ratio.
 */
import { Aggregate, Headlines, Secondary } from "../../components/Aggregate";
import { Lend, type Lender } from "../../components/diagram/Lend";
import { StatTile } from "../../components/StatTile";
import { FAILED_OPEN_GLOSS } from "../Overview";
import styles from "./memory.module.css";

type Props = {
  /** The page's diagram takes each figure's own `Leaf` back through this (decision 42). */
  lend: Lender;
  /** A fresh token when the watched stream says a count may have moved. */
  refresh: object | undefined;
};

export function MemoryFigures({ lend, refresh }: Props) {
  return (
    <div className={styles.figures}>
      <Aggregate route="/v1/events/gate" title="Memory check" headlines={3} refresh={refresh}>
        {(wire) => (
          <>
            <Lend name="skip" leaf={wire.at(".skip")} to={lend} />
            <Lend name="retrieve" leaf={wire.at(".retrieve")} to={lend} />
            <Lend name="failed open" leaf={wire.at(".failed_open")} to={lend} />
            <Headlines>
              <StatTile label="Skipped" leaf={wire.at(".skip")} />
              <StatTile label="Retrieved" leaf={wire.at(".retrieve")} />
              <StatTile label="Failed open" leaf={wire.at(".failed_open")} />
            </Headlines>
            <p className={styles.gloss}>{FAILED_OPEN_GLOSS}</p>
          </>
        )}
      </Aggregate>
      <Aggregate route="/v1/memory/totals" title="Memory" headlines={2} refresh={refresh}>
        {(wire) => (
          <>
            <Lend name="facts active" leaf={wire.at(".facts_active")} to={lend} />
            <Lend name="facts archived" leaf={wire.at(".facts_archived")} to={lend} />
            <Lend name="episodes" leaf={wire.at(".episodes")} to={lend} />
            <Lend name="procedures active" leaf={wire.at(".procedures_active")} to={lend} />
            <Headlines>
              <StatTile label="Facts" leaf={wire.at(".facts_active")} />
              <StatTile label="Procedures" leaf={wire.at(".procedures_active")} />
            </Headlines>
            <Secondary>
              <StatTile size="secondary" label="Archived facts" leaf={wire.at(".facts_archived")} />
              <StatTile size="secondary" label="Episodes" leaf={wire.at(".episodes")} />
            </Secondary>
          </>
        )}
      </Aggregate>
    </div>
  );
}
