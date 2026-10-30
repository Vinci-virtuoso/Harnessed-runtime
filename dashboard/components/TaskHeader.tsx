"use client";

/**
 * Above the conversation of a session that belongs to a task: the task's text, its state in the
 * human words, the stop or pause reason when there is one, and whether the task counts as
 * unattended (FR-154, UJ-24, UJ-26; design.md "The console in v6").
 *
 * Every word is a served field through `lib/human-words.ts`: the state, the stop reason, the pause
 * reason; "paused once" is read off the served `pauses` text, never counted here (MUST rule 21).
 */
import { humanWord } from "../lib/human-words";
import type { Human } from "../lib/humanize";
import { leafIfSent } from "../lib/rows";
import type { Leaf, Wire } from "../lib/wire";

import type { ReactNode } from "react";

import { PanelFrame } from "./PanelFrame";
import { StopLine } from "./UncertainStop";
import { Value } from "./Value";
import styles from "./TaskHeader.module.css";

export const TASK_TITLE = "Task";

/** The task's own read, its id placed verbatim (MUST rule 25). */
export function taskRoute(taskId: Leaf): string {
  return `/v1/tasks/${encodeURIComponent(taskId.text)}`;
}

/** The leaf's display words from the table, else the wire's own word, verbatim. */
export function wordFor(field: string, leaf: Leaf): Human {
  return { text: humanWord(field, leaf.text) ?? leaf.text, exact: leaf.text };
}

function served(wire: Wire, path: string): Leaf | undefined {
  const leaf = leafIfSent(wire, path);
  return leaf === undefined || leaf.kind === "null" ? undefined : leaf;
}

/** What the served `unattended` says, in words; nothing before the task is done. */
function attendance(wire: Wire): string | undefined {
  const unattended = served(wire, ".unattended");
  if (unattended === undefined) return undefined;
  if (unattended.text === "true") return "counted as unattended";
  const pauses = served(wire, ".pauses");
  if (pauses !== undefined && pauses.text !== "0") {
    const times = pauses.text === "1" ? "once" : `${pauses.text} times`;
    return `not counted as unattended: paused ${times} for you`;
  }
  return "not counted as unattended: you wrote to it";
}

/** The paused-on-uncertain line from the served `pause_*` leaves; nothing when none was compared. */
export function pauseStopLine(wire: Wire): ReactNode {
  const figure = served(wire, ".pause_confidence_percent");
  const floor = served(wire, ".pause_floor_percent");
  if (figure === undefined || floor === undefined) return undefined;
  return <StopLine figure={figure} compared={served(wire, ".pause_compared")} floor={floor} />;
}

type Props = {
  taskId: Leaf;
  /** A refetch signal from the run this session is watching (FR-75). */
  refresh?: object;
};

export function TaskHeader({ taskId, refresh }: Props) {
  return (
    <PanelFrame route={taskRoute(taskId)} title={TASK_TITLE} sourceLabel="Tasks: this task" refresh={refresh} skeletonRows={1}>
      {(wire) => {
        const state = wire.at(".state");
        const reason = served(wire, ".pause_reason") ?? served(wire, ".stop_reason");
        const reasonField = served(wire, ".pause_reason") === undefined ? "task_stop_reason" : "task_pause_reason";
        const note = attendance(wire);
        const pauseLine = pauseStopLine(wire);
        // A stop reason beside "Done" says nothing the word does not; a failure's reason is the news.
        const showReason = reason !== undefined && state.text !== "done";
        return (
          <header className={styles.header} data-testid="task-header">
            <p className={styles.text}>
              <Value leaf={wire.at(".text")} />
            </p>
            <p className={styles.line}>
              <span className={styles.state} data-state={state.text}>
                <Value leaf={state} human={wordFor("task_state", state)} />
              </span>
              {showReason ? (
                <>
                  {": "}
                  <Value leaf={reason} human={wordFor(reasonField, reason)} />
                </>
              ) : null}
              {pauseLine === undefined ? null : (
                <span className={styles.note} data-testid="task-pause-line">
                  {pauseLine}
                </span>
              )}
              {note === undefined ? null : <span className={styles.note}>{note}</span>}
            </p>
          </header>
        );
      }}
    </PanelFrame>
  );
}
