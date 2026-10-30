"use client";

/**
 * The Overview's "Recent activity": `GET /v1/messages?limit=` in the gateway's own order (FR-128,
 * FR-129; decision 66; `ux/design.md`'s `ActivityList` row). It replaces "Latest turns" - a
 * `DataTable` of run keys - with a list titled by what the operator actually said.
 *
 * **0 run keys, ISO timestamps or stop words in a row.** The session id builds the row's link
 * (MUST rule 25, verbatim) but is never rendered as text, and `created_at` never rides on the row
 * at all: both stay reachable only through a figure's own tooltip and the drawer a `Value` opens
 * (`D-9`). The row's own link is not a `Value` - a `Value` is a `button`, and a `button` inside an
 * `a` is a nested control no keyboard handles sensibly, the same reasoning `DataTable`'s row link
 * already follows - so the title is `text`'s own characters, read directly as the anchor's plain
 * content, with the figure's `data-route`/`data-path` kept on the anchor for provenance.
 */
import { now } from "../lib/clock";
import { humanWord } from "../lib/human-words";
import { humanTimestamp } from "../lib/humanize";
import type { Leaf, Wire } from "../lib/wire";
import { rowsOf } from "../lib/rows";

import { exchangeAnchor } from "./Exchange";
import { PanelFrame } from "./PanelFrame";
import { Value } from "./Value";
import styles from "./ActivityList.module.css";

/** The panel's own name, exported so a test names the region rather than guessing at it. */
export const ACTIVITY_TITLE = "Recent activity";

/** design.md's own empty-state sentence, beside `EmptyState`'s generic "No recent activity
 * recorded yet." headline (`D-3`). */
const NOTHING_RUN = "Nothing has run yet.";

/** The write command that would put a row here - `harnessed run`, the same one `EmptyState`'s
 * "Create some with" line named before ticket 187 (LatestTurns), never the read twin `Value`'s own
 * drawer already shows for a row's figure (`lib/cli-twins.ts`'s `/v1/messages` entry). */
const CREATE_TWIN = "harnessed run";

/** The served `author` of a message the harness sent on a task's behalf. */
const TASK_AUTHOR = "task";

/** The gateway route for a limit, as the route table spells it. */
export function activityRoute(limit: string): string {
  return `/v1/messages?limit=${limit}`;
}

/** A client-owned `?limit=` choice, as the `Leaf` this panel is handed (decision 66). */
export function activityLimitLeaf(text: string): Leaf {
  return { kind: "string", text, path: "limit", route: "/" };
}

/** The console at the exchange a row named: the session id placed verbatim and URL-encoded once (MUST rule 25), then the served `message_seq` as the exchange's own anchor. */
function consoleHref(sessionId: string, messageSeq: Leaf): string {
  return `/console?session=${encodeURIComponent(sessionId)}#${exchangeAnchor(messageSeq)}`;
}

type Props = {
  /** The client-owned `?limit=` choice. Spelled into the route, never counted. */
  limit: Leaf;
  /** A refetch signal from the Overview's watched stream (FR-75): a new object is one more read. */
  refresh?: object;
};

export function ActivityList({ limit, refresh }: Props) {
  return (
    <PanelFrame
      route={activityRoute(limit.text)}
      title={ACTIVITY_TITLE}
      sourceLabel="Ledger: all time"
      cliTwin={CREATE_TWIN}
      emptyText={NOTHING_RUN}
      refresh={refresh}
    >
      {(wire) => {
        const rows = rowsOf(wire, (index) => `.messages[${index}].session_id`);
        if (rows.length === 0) return null;
        return (
          <ul className={styles.list} data-testid="activity-list">
            {rows.map((row) => (
              <ActivityRow key={row} wire={wire} row={row} />
            ))}
          </ul>
        );
      }}
    </PanelFrame>
  );
}

function ActivityRow({ wire, row }: { wire: Wire; row: number }) {
  const text = wire.at(`.messages[${row}].text`);
  const handler = wire.at(`.messages[${row}].handler`);
  const outcome = wire.at(`.messages[${row}].outcome`);
  const cost = wire.at(`.messages[${row}].cost`);
  const created = wire.at(`.messages[${row}].created_at`);
  const sessionId = wire.at(`.messages[${row}].session_id`).text;
  const messageSeq = wire.at(`.messages[${row}].message_seq`);
  const author = wire.at(`.messages[${row}].author`);
  // The served author says a task wrote this message; the chip never infers it (G-37).
  const fromTask = author.kind !== "null" && author.text === TASK_AUTHOR;
  const outcomeWord = outcome.kind === "null" ? undefined : humanWord("outcome", outcome.text);
  const title = text.kind === "null" ? "unknown" : text.text;

  return (
    <li className={styles.row}>
      <a
        className={styles.title}
        href={consoleHref(sessionId, messageSeq)}
        data-figure=""
        data-route={text.route}
        data-path={text.path}
        aria-label={outcomeWord === undefined ? title : `${title} - ${outcomeWord}`}
      >
        {title}
      </a>
      <p className={styles.meta}>
        {fromTask ? (
          <span className={styles.chip}>
            <Value leaf={author} human={{ text: "Task", exact: author.text }} />
          </span>
        ) : null}
        <Value leaf={handler} />
        <Value leaf={outcome} human={outcomeWord === undefined ? undefined : { text: outcomeWord, exact: outcome.text }} />
        <Value leaf={cost} />
        <span className={styles.time}>
          <Value leaf={created} human={humanTimestamp(created, now())} />
        </span>
      </p>
    </li>
  );
}
