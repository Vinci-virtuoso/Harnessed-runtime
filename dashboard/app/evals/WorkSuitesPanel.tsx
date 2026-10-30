"use client";

/**
 * `/evals`, "Work suites": the suites the operator records from his own sessions, with how many
 * cases still await his verdict and the latest score (FR-139, UJ-9, UJ-23).
 *
 * One read of `/v1/eval/suites`, which serves every suite; a row is drawn for a suite whose served
 * `kind` is the word `work` and for no other, in the gateway's order. With no work suite the panel
 * draws nothing at all, heading included (`quiet`).
 */
import { PanelFrame } from "../../components/PanelFrame";
import { WorkSuiteRow } from "../../components/WorkSuiteRow";
import { rowsOf } from "../../lib/rows";
import type { Wire } from "../../lib/wire";

import styles from "./evals.module.css";

export const PANEL_TITLE = "Work suites";
export const SUITES_ROUTE = "/v1/eval/suites";

/** The served `kind` of a suite recorded from sessions (`evals/suite.py::KIND_WORK`). */
const WORK = "work";

function isWork(wire: Wire, row: number): boolean {
  return wire.at(`.suites[${row}].kind`).text === WORK;
}

export function WorkSuitesPanel() {
  return (
    <PanelFrame
      route={SUITES_ROUTE}
      title={PANEL_TITLE}
      sourceLabel="Suites on disk, ledger: all time"
      cliTwin="harnessed eval suites"
      quiet
    >
      {(wire) => {
        const work = rowsOf(wire, (index) => `.suites[${index}].suite`).filter((row) => isWork(wire, row));
        if (work.length === 0) return null;
        return (
          <ul className={styles.suites} aria-label={PANEL_TITLE}>
            {work.map((row) => (
              <WorkSuiteRow key={row} wire={wire} row={row} />
            ))}
          </ul>
        );
      }}
    </PanelFrame>
  );
}
