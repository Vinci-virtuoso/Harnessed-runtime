"use client";

/**
 * "This task", above "This session" in the console's side rail: each used figure beside its budget
 * as **two** served values with the word "of" between them - never a bar, a percentage or a
 * remainder (`D-1`) - then who wrote to the task, how often it paused, who started it, when it was
 * queued, and the two things an operator copies: its id and the command that prints it (FR-154,
 * design.md "TaskRail").
 */
import {
  humanBudgetSeconds,
  humanClock,
  humanDollars,
  humanSeconds,
} from "../lib/humanize";
import { type ReactNode, useCallback } from "react";

import type { PanelResult } from "../lib/fetch-panel";
import type { Leaf, Wire } from "../lib/wire";

import { CopyLine, copy } from "./CopyLine";
import { PanelFrame } from "./PanelFrame";
import { pauseStopLine, taskRoute, wordFor } from "./TaskHeader";
import { Value } from "./Value";
import styles from "./TaskRail.module.css";

export const TASK_RAIL_TITLE = "This task";
export const BUDGET_SPENT = "Budget spent";

/** What the console needs from the task's own read besides the figures it draws here. */
export type TaskRow = { state: Leaf; pause: Leaf; cwd: Leaf; entry: Leaf; created: Leaf };

type Props = {
  taskId: Leaf;
  refresh?: object;
  /** Reports each settled read that reached `ok`, so the composer and the repository card can say what the task says. */
  onRow?: (row: TaskRow) => void;
};

export function TaskRail({ taskId, refresh, onRow }: Props) {
  // Stable, so the panel reports each settled read once and not once per render of this parent.
  const settled = useCallback(
    (result: PanelResult) => {
      if (result.state !== "ok") return;
      onRow?.({
        state: result.wire.at(".state"),
        pause: result.wire.at(".pause_reason"),
        cwd: result.wire.at(".cwd"),
        entry: result.wire.at(".entry"),
        created: result.wire.at(".created_at"),
      });
    },
    [onRow],
  );
  return (
    <PanelFrame
      route={taskRoute(taskId)}
      title={TASK_RAIL_TITLE}
      sourceLabel="Tasks: this task"
      refresh={refresh}
      onSettled={settled}
    >
      {(wire) => <Fields wire={wire} />}
    </PanelFrame>
  );
}

function Fields({ wire }: { wire: Wire }) {
  const state = wire.at(".state");
  const stopReason = wire.at(".stop_reason");
  const id = wire.at(".task_id");
  const startedBy = wire.at(".started_by");
  const created = wire.at(".created_at");
  const pauseLine = pauseStopLine(wire);
  const command = `harnessed task show ${id.text} --json`;
  return (
    <div className={styles.rail} data-testid="task-rail">
      <dl className={styles.fields}>
        <Row label="State">
          <Value leaf={state} human={wordFor("task_state", state)} />
          {stopReason.kind !== "null" && stopReason.text === "budget" ? (
            <span className={styles.spent}>{BUDGET_SPENT}</span>
          ) : null}
        </Row>
        <Row label="Steps">
          <Of
            used={<Value leaf={wire.at(".used_iterations")} />}
            budget={<Value leaf={wire.at(".budget_iterations")} />}
          />
        </Row>
        <Row label="Time">
          <Of
            used={<Value leaf={wire.at(".used_seconds")} human={humanSeconds(wire.at(".used_seconds"))} />}
            budget={
              <Value leaf={wire.at(".budget_seconds")} human={humanBudgetSeconds(wire.at(".budget_seconds"))} />
            }
          />
        </Row>
        <Row label="Cost">
          <Of
            used={<Value leaf={wire.at(".used_dollars")} human={humanDollars(wire.at(".used_dollars"))} />}
            budget={
              <Value leaf={wire.at(".budget_dollars")} human={humanDollars(wire.at(".budget_dollars"))} />
            }
          />
        </Row>
        <Row label="Messages from you">
          <Value leaf={wire.at(".human_messages")} />
        </Row>
        <Row label="Pauses">
          <Value leaf={wire.at(".pauses")} />
        </Row>
        {pauseLine === undefined ? null : (
          <Row label="Paused">
            <span data-testid="rail-pause-line">{pauseLine}</span>
          </Row>
        )}
        <Row label="Started by">
          {startedBy.kind === "null" ? (
            <Value leaf={startedBy} />
          ) : (
            <Value leaf={startedBy} human={wordFor("task_started_by", startedBy)} />
          )}
        </Row>
        <Row label="Queued">
          <Value leaf={created} human={humanClock(created)} />
        </Row>
      </dl>
      <div className={styles.copies}>
        <button type="button" className={styles.copyId} onClick={() => copy(id.text)}>
          Copy id
        </button>
        <CopyLine line={command} label="Copy" testId="task-show-command" />
      </div>
    </div>
  );
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className={styles.row}>
      <dt className={styles.label}>{label}</dt>
      <dd className={styles.figure}>{children}</dd>
    </div>
  );
}

/** Two served values with the word between them: the whole of what a budget looks like here. */
function Of({ used, budget }: { used: ReactNode; budget: ReactNode }) {
  return (
    <>
      {used} <span className={styles.of}>of</span> {budget}
    </>
  );
}
