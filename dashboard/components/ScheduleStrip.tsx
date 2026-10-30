"use client";

/**
 * Traces' "Schedule": `GET /v1/schedules`, one row per schedule, first on the page (FR-155, G-41;
 * `ux/design.md`'s `ScheduleStrip`).
 *
 * The rule is the served `rule_words`; where it is installed is the served `installed` and
 * `where`, with "Not installed" in `color-warn`; "last ran 03:00" and the next due time are the
 * served instants, the next one drawn only where the response sent one and never computed from the
 * rule (`D-1`). The copy line is the real command (`V6-3`).
 */
import { humanWord } from "../lib/human-words";
import { humanClock } from "../lib/humanize";
import { nullReasonFor } from "../lib/null-reasons";
import { rowsOf } from "../lib/rows";

import { CopyLine } from "./CopyLine";
import { PanelFrame } from "./PanelFrame";
import { Value } from "./Value";
import styles from "./ScheduleStrip.module.css";

export const SCHEDULE_TITLE = "Schedule";
export const SCHEDULE_ROUTE = "/v1/schedules";
export const SCHEDULE_LIST_COMMAND = "harnessed schedule list --json";
export const SCHEDULE_ADD_COMMAND = "harnessed schedule add --every 6h";

const INSTALLED_WORDS = "Installed in Task Scheduler";
const NOT_INSTALLED_WORDS = "Not installed";

export function ScheduleStrip({ refresh }: { refresh?: object }) {
  return (
    <PanelFrame route={SCHEDULE_ROUTE} title={SCHEDULE_TITLE} sourceLabel="Schedules: all time" refresh={refresh}>
      {(wire) => {
        const rows = rowsOf(wire, (index) => `.schedules[${index}].schedule_id`);
        if (rows.length === 0) {
          return (
            <p className={styles.none} data-testid="schedule-none">
              No schedule. Add one with <code className={styles.code}>{SCHEDULE_ADD_COMMAND}</code>
            </p>
          );
        }
        return (
          <div className={styles.body}>
            <ul className={styles.list} data-testid="schedule-list">
              {rows.map((row) => {
                const at = (field: string) => wire.at(`.schedules[${row}].${field}`);
                const installed = at("installed");
                const where = at("where");
                const lastRan = at("last_run_at");
                const nextDue = at("next_due_at");
                const isInstalled = installed.text === "true";
                const backend = isInstalled && where.kind !== "null" ? humanWord("schedule_where", where.text) : undefined;
                return (
                  <li key={row} className={styles.row} data-testid="schedule-row">
                    <span className={styles.rule}>
                      <Value leaf={at("rule_words")} />
                    </span>
                    <span className={isInstalled ? styles.where : styles.warn}>
                      <Value
                        leaf={isInstalled && (where.text === "task_scheduler" || backend) ? where : installed}
                        human={{
                          text: isInstalled && where.text === "task_scheduler" ? INSTALLED_WORDS : backend ?? (isInstalled ? "Installed" : NOT_INSTALLED_WORDS),
                          exact: isInstalled && (where.text === "task_scheduler" || backend) ? where.text : installed.text,
                        }}
                      />
                    </span>
                    <span className={styles.when}>
                      last ran{" "}
                      <Value
                        leaf={lastRan}
                        human={lastRan.kind === "null" ? undefined : humanClock(lastRan)}
                        nullReason={nullReasonFor(wire.route, ".schedules[].last_run_at")}
                      />
                    </span>
                    {nextDue.kind === "null" ? null : (
                      <span className={styles.when}>
                        next due <Value leaf={nextDue} human={humanClock(nextDue)} />
                      </span>
                    )}
                  </li>
                );
              })}
            </ul>
            <CopyLine line={SCHEDULE_LIST_COMMAND} label="Copy" testId="schedule-command" />
          </div>
        );
      }}
    </PanelFrame>
  );
}
