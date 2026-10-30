"use client";

/**
 * Traces' "Tasks": `GET /v1/tasks?limit=` newest first as served, above Sessions (FR-155, `V6-6`;
 * `ux/design.md`'s `TaskList`).
 *
 * Each row carries the task's text, its state word, the stop or pause reason, who started it, its
 * handler and outcome (served on the task row), its cost and a relative time. A task that has not
 * started reads "Not started yet" and has no `Open`; every other row opens its session. The words
 * come from `HUMAN_WORDS`; a word it does not know renders as served. Nothing is ordered, counted
 * or summed here (`D-1`).
 */
import { now } from "../lib/clock";
import { humanWord } from "../lib/human-words";
import { humanAgo, humanDollars } from "../lib/humanize";
import { nullReasonFor } from "../lib/null-reasons";
import { rowsOf } from "../lib/rows";
import type { Leaf, Wire } from "../lib/wire";

import { CopyLine } from "./CopyLine";
import { PanelFrame } from "./PanelFrame";
import { Value } from "./Value";
import styles from "./TaskList.module.css";

export const TASKS_LIST_TITLE = "Tasks";
export const TASK_HELP_COMMAND = "harnessed task add --help";

/** The gateway route for a limit, as the route table spells it. */
export function tasksRoute(limit: string): string {
  return `/v1/tasks?limit=${limit}`;
}

/** A served word through `HUMAN_WORDS`, as `Value`'s human rendering; undefined where the table has no row. */
function worded(field: string, leaf: Leaf, prefix = "") {
  if (leaf.kind === "null") return undefined;
  const words = humanWord(field, leaf.text);
  if (words === undefined) return undefined;
  const text = prefix === "" ? words : `${prefix}${words.charAt(0).toLowerCase()}${words.slice(1)}`;
  return { text, exact: leaf.text };
}

export function TaskList({ limit, refresh }: { limit: string; refresh?: object }) {
  return (
    <PanelFrame
      route={tasksRoute(limit)}
      title={TASKS_LIST_TITLE}
      sourceLabel="Tasks: all time"
      refresh={refresh}
    >
      {(wire) => {
        const rows = rowsOf(wire, (index) => `.tasks[${index}].task_id`);
        if (rows.length === 0) {
          return (
            <div className={styles.empty} data-testid="tasks-empty">
              <p className={styles.line}>No tasks yet. Queue one with:</p>
              <CopyLine line={TASK_HELP_COMMAND} label="Copy" testId="tasks-command" />
            </div>
          );
        }
        return (
          <ul className={styles.list} data-testid="task-list">
            {rows.map((row) => (
              <TaskRow key={row} wire={wire} row={row} />
            ))}
          </ul>
        );
      }}
    </PanelFrame>
  );
}

function TaskRow({ wire, row }: { wire: Wire; row: number }) {
  const at = (field: string) => wire.at(`.tasks[${row}].${field}`);
  const state = at("state");
  const stop = at("stop_reason");
  const pause = at("pause_reason");
  const startedBy = at("started_by");
  const handler = at("handler");
  const outcome = at("outcome");
  const session = at("session_id");
  const created = at("created_at");
  const queued = state.text === "queued";
  const reason =
    stop.kind !== "null"
      ? worded("task_stop_reason", stop)
      : pause.kind !== "null"
        ? worded("task_pause_reason", pause)
        : undefined;
  const reasonLeaf = stop.kind !== "null" ? stop : pause;

  // The headline figure is the row's one Tab stop; the rest are reached in its drawer (decision 103).
  return (
    <li className={styles.row} data-testid="task-row">
      <p className={styles.headline}>
        <Value leaf={at("text")} />
      </p>
      <p className={styles.meta}>
        <span className={state.text === "failed" ? styles.failed : state.text === "needs_human" ? styles.warn : undefined}>
          <Value tabIndex={-1} leaf={state} human={worded("task_state", state)} />
        </span>
        {reasonLeaf.kind === "null" ? null : <Value tabIndex={-1} leaf={reasonLeaf} human={reason} />}
        <Value tabIndex={-1} leaf={startedBy} human={worded("task_started_by_phrase", startedBy, "started by ")} />
        {queued ? (
          <span>Not started yet</span>
        ) : (
          <>
            <Value tabIndex={-1} leaf={handler} nullReason={nullReasonFor(wire.route, ".tasks[].handler")} />
            <Value
              tabIndex={-1}
              leaf={outcome}
              human={worded("outcome", outcome)}
              nullReason={nullReasonFor(wire.route, ".tasks[].outcome")}
            />
          </>
        )}
        <Value tabIndex={-1} leaf={at("used_dollars")} human={humanDollars(at("used_dollars"))} />
        <span className={styles.time}>
          <Value tabIndex={-1} leaf={created} human={humanAgo(created, now())} />
        </span>
        {queued || session.kind === "null" ? null : (
          <a
            className={styles.open}
            tabIndex={-1}
            href={`/console?session=${encodeURIComponent(session.text)}`}
            aria-label={`Open ${at("text").text}`}
          >
            Open
          </a>
        )}
      </p>
    </li>
  );
}
