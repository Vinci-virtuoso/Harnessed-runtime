"use client";

/**
 * Under the composer after "Queue as a task" is answered: the text, the budget the task got, the
 * line that runs it from a terminal and a way to find it in Traces (FR-154, UJ-24; design.md
 * "QueuedNotice"). It reads the `201` of `POST /control/v1/tasks` and nothing else - the console
 * never starts the task - and the budget is the answer's own, "the default" included.
 */
import { useContext, useEffect, useState } from "react";

import { fetchPanel } from "../lib/fetch-panel";
import { humanBudgetSeconds, humanClock, humanDollars } from "../lib/humanize";
import { humanWord } from "../lib/human-words";
import { rowsOf } from "../lib/rows";
import type { Leaf, Wire } from "../lib/wire";

import { CopyLine } from "./CopyLine";
import { GatewayOriginContext } from "./PanelFrame";
import { Value } from "./Value";
import styles from "./QueuedNotice.module.css";

export const QUEUED_TITLE = "Queued as a task";
export const TRACES_LINK = "See it in Traces";
export const SCHEDULES_ROUTE = "/v1/schedules";
export const RUN_DUE_COMMAND = "harnessed task run --due";

/** The next run of the first installed schedule `/v1/schedules` serves, or nothing when none is. */
function useNextRun(): Leaf | undefined {
  const origin = useContext(GatewayOriginContext);
  const [next, setNext] = useState<Leaf | undefined>(undefined);
  useEffect(() => {
    const controller = new AbortController();
    let live = true;
    void fetchPanel(SCHEDULES_ROUTE, origin, controller.signal).then((result) => {
      if (!live || result.state !== "ok") return;
      const { wire } = result;
      for (const row of rowsOf(wire, (index) => `.schedules[${index}].schedule_id`)) {
        const due = wire.at(`.schedules[${row}].next_due_at`);
        if (wire.at(`.schedules[${row}].installed`).text === "true" && due.kind !== "null") {
          setNext(due);
          return;
        }
      }
    });
    return () => {
      live = false;
      controller.abort();
    };
  }, [origin]);
  return next;
}

type Props = {
  /** What was queued: the string the operator typed. */
  text: string;
  /** The `201` body, whole. */
  wire: Wire;
};

export function QueuedNotice({ text, wire }: Props) {
  const iterations = wire.at(".budget_iterations");
  const seconds = wire.at(".budget_seconds");
  const dollars = wire.at(".budget_dollars");
  const source = wire.at(".budget_source");
  const runCommand = `harnessed task run ${wire.at(".task_id").text}`;
  const nextRun = useNextRun();
  const words = humanWord("budget_source", source.text) ?? source.text;
  return (
    <section className={styles.notice} role="status" aria-live="polite" aria-label={QUEUED_TITLE} data-testid="queued-notice">
      <p className={styles.title}>{QUEUED_TITLE}</p>
      <p className={styles.text}>{text}</p>
      <p className={styles.budget}>
        {"Budget: "}
        <Value leaf={iterations} /> steps, <Value leaf={seconds} human={humanBudgetSeconds(seconds)} />,{" "}
        <Value leaf={dollars} human={humanDollars(dollars)} />, {words}
      </p>
      {nextRun === undefined ? (
        <>
          <p className={styles.budget} data-testid="queued-unscheduled">
            Nothing is scheduled. Run the queue from a terminal:
          </p>
          <CopyLine line={RUN_DUE_COMMAND} label="Copy" testId="task-run-due-command" />
          <p className={styles.budget}>Or run just this task:</p>
        </>
      ) : (
        <p className={styles.budget} data-testid="queued-next-run">
          {"It runs on the next scheduled run, "}
          <Value leaf={nextRun} human={humanClock(nextRun)} />
          {", or now from a terminal:"}
        </p>
      )}
      <CopyLine line={runCommand} label="Copy" testId="task-run-command" />
      <a className={styles.link} href="/traces">
        {TRACES_LINK}
      </a>
    </section>
  );
}
