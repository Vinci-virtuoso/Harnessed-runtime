"use client";

/**
 * `/traces` - every session and every loop run the dashboard can read back (FR-55, FR-59, FR-64,
 * FR-128, FR-129; UJ-7, UJ-11).
 *
 * A client component holding no data (decision 34): it reads the query and nothing else, and
 * every figure below it is fetched in the browser, so no gateway value is ever in the
 * server-rendered payload. **`?session=` renders that session's transcript and live stream,
 * `?key=` renders that run and its trace, neither renders the two listings stacked** (decision
 * 45): ids ride in the query because an export cannot enumerate what a database holds. `?limit=`
 * is passed down as its characters, unvalidated (whether a limit is servable is the gateway's
 * answer to give), and one `?limit=` drives both listings. Ids are passed exactly as the URL
 * carried them (MUST rules 15, 25).
 */
import { useSearchParams } from "next/navigation";

import { ScheduleStrip } from "../../components/ScheduleStrip";
import { TaskList } from "../../components/TaskList";
import { RunPanel } from "./RunPanel";
import { RunsPanel } from "./RunsPanel";
import { SessionLookup } from "./SessionLookup";
import { SessionPanels } from "./SessionPanels";
import { SessionsPanel } from "./SessionsPanel";
import { TracePanel } from "./TracePanel";
import { DEFAULT_LIMIT } from "./limit";
import styles from "./sessions.module.css";

export default function TracesPage() {
  const query = useSearchParams();
  const limit = query.get("limit") ?? undefined;
  const session = query.get("session") ?? undefined;
  const key = query.get("key") ?? undefined;

  if (session !== undefined) {
    return (
      <div className={styles.page}>
        <h1 className={styles.title}>Session</h1>
        <p className={styles.subtitle}>
          What the events table already holds for this session, and every event that lands while
          this page is open.
        </p>
        <p className={styles.identity} data-testid="session-identity">
          {session}
        </p>
        <SessionPanels sessionId={session} />
      </div>
    );
  }

  if (key !== undefined) {
    return (
      <div className={styles.page}>
        <h1 className={styles.title}>Run</h1>
        <p className={styles.subtitle}>
          What the run journal recorded under this key, and the turns the loop took to get there.
        </p>
        <RunPanel runKey={key} />
        <TracePanel runKey={key} />
      </div>
    );
  }

  return (
    <div className={styles.page}>
      <h1 className={styles.title}>Traces</h1>
      <p className={styles.subtitle}>
        Every session the events table holds and every loop run the journal recorded, newest first.
      </p>
      <div className={styles.section}>
        <ScheduleStrip />
      </div>
      <div className={styles.section}>
        <TaskList limit={limit ?? DEFAULT_LIMIT} />
      </div>
      <div className={styles.section}>
        <SessionsPanel limit={limit ?? DEFAULT_LIMIT} />
        <SessionLookup />
      </div>
      <div className={styles.section}>
        <RunsPanel limit={limit ?? DEFAULT_LIMIT} />
      </div>
    </div>
  );
}
