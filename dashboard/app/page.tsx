/**
 * `/` - the Overview (FR-75, FR-54, FR-129; UJ-12, UJ-14, UJ-15).
 *
 * A server component holding no data (decision 34): the figures are fetched in the browser by the
 * panels `Overview` renders, so no gateway value is ever in the server-rendered payload and every
 * fetch of a gateway route is visible in a network log. Six aggregate panels, the diagram, the
 * newest session's `TraceTimeline` as Latest run, then `ActivityList` as Recent activity (ticket
 * 187, replacing "Latest turns"). FR-54's health panel moved to the Models page's `SystemSection`
 * (ticket 186). The shell around it is mounted by `app/layout.tsx`.
 */
import { Overview } from "./Overview";
import styles from "./page.module.css";

export default function Home() {
  return (
    <div className={styles.page}>
      <h1 className={styles.title}>Overview</h1>
      <p className={styles.subtitle}>What the harness has done, all time, counted by the gateway.</p>
      <Overview />
    </div>
  );
}
