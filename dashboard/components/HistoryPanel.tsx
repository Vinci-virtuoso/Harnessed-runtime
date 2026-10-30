"use client";

/**
 * The console's `HISTORY`: the sessions listing, titled by what the operator said (FR-122, FR-128,
 * UJ-16; decision 66). Replaces the v3/v4 `DataTable` of run keys, first events and timestamps
 * with one row per session, titled by the served `title` - the same shape `ActivityList` (ticket
 * 187) already gave the Overview's own listing, applied here to `/v1/sessions?limit=`.
 *
 * **HISTORY is not a table and not a store.** It is `GET /v1/sessions?limit=` (FR-48, gaining
 * `title` in v5) in the gateway's own order, with the `?limit=` control the Sessions page already
 * has. Every row the gateway sends is a row here: `first_event` filters nothing, so an eval or
 * improve session is listed like any other, and only its row action differs.
 *
 * **0 run keys, ISO timestamps or stop words in a row.** `session_id`, `first_ts` and
 * `terminal_event` build the row's `href` and its `Details` chip alone; they are never rendered as
 * row text (MUST rule 25, verbatim; `D-9`). Opening `Details` shows them, exactly as any other
 * gateway figure's provenance drawer does. A session recorded before the router carries `title:
 * null` and is titled by its own null reason, never guessed.
 *
 * **Four served strings per routed row** (ticket 232, `V6-9`): `last_handler`, `last_outcome`,
 * `last_cost` and `last_at`, each the listing's own text with **0** derivation here. `last_at` is
 * a `humanTimestamp` of the served time, never the ISO string; a session that never routed a
 * message serves all four as `null` and shows none of them.
 */
import { useRouter, useSearchParams } from "next/navigation";
import { useId, useState } from "react";

import { LIMITS, RUN_COMMAND, footerLine, listingRoute } from "../app/traces/limit";
import { now } from "../lib/clock";
import { humanWord } from "../lib/human-words";
import { humanTimestamp } from "../lib/humanize";
import { rowsOf } from "../lib/rows";
import { nullReasonFor } from "../lib/null-reasons";
import type { Leaf, Wire } from "../lib/wire";

import { PanelFrame } from "./PanelFrame";
import { SourceChip } from "./SourceChip";
import { Value } from "./Value";
import styles from "./HistoryPanel.module.css";

/** The panel's name: its toggle's label and its region's title. */
export const HISTORY_TITLE = "History";
/** A `run.started` row's action. */
export const RESUME = "resume";
/** Every other row's action: an eval or improve session is listed, and is not a run to continue. */
export const CANNOT_CONTINUE = "cannot be continued";
/** The row's own drawer trigger: the run key, the ISO time and the stop word, on request. */
const DETAILS = "Details";

/** The one start a console can continue: `harnessed run`'s own. */
const CONTINUABLE_START = "run.started";

/** Where the console lives, and the page every row action and limit choice returns to. */
const CONSOLE_PATH = "/console";

/** The listing route at a limit, as the route table spells it. */
export function historyRoute(limit: string): string {
  return listingRoute(limit);
}

/** The console at one session, the id placed verbatim and URL-encoded once (MUST rule 25). */
export function resumeHref(sessionId: string, limit: string): string {
  return `${CONSOLE_PATH}?${new URLSearchParams({ limit, session: sessionId }).toString()}`;
}

/** The `?limit=` the console's URL carries, as the `Leaf` the panel is handed. */
export function limitLeaf(text: string): Leaf {
  return { kind: "string", text, path: "limit", route: CONSOLE_PATH };
}

type Props = {
  /** The client-owned `?limit=` choice. Spelled into the route and the footer, never counted. */
  limit: Leaf;
};

export function HistoryPanel({ limit }: Props) {
  const [open, setOpen] = useState(false);
  const bodyId = useId();

  return (
    <section className={styles.history}>
      <button
        type="button"
        className={styles.toggle}
        aria-expanded={open}
        aria-controls={bodyId}
        onClick={() => setOpen(!open)}
      >
        <span className={styles.chevron} aria-hidden="true">
          {open ? "▾" : "▸"}
        </span>
        {HISTORY_TITLE}
      </button>
      {open ? (
        <div id={bodyId}>
          <Listing limit={limit} />
        </div>
      ) : null}
    </section>
  );
}

/** The listing itself, fetched only once the operator has opened the panel. */
function Listing({ limit }: Props) {
  return (
    <PanelFrame
      route={historyRoute(limit.text)}
      title={HISTORY_TITLE}
      sourceLabel="Ledger: all time"
      cliTwin={RUN_COMMAND}
    >
      {(wire) => {
        const rows = rowsOf(wire, (index) => `.sessions[${index}].session_id`);
        if (rows.length === 0) return null;
        return (
          <div className={styles.body}>
            <LimitControl limit={limit} />
            <ul className={styles.list} data-testid="history-list">
              {rows.map((row) => (
                <HistoryRow key={row} wire={wire} row={row} limit={limit} />
              ))}
            </ul>
            <p className={styles.footer}>{footerLine(limit.text)}</p>
          </div>
        );
      }}
    </PanelFrame>
  );
}

function HistoryRow({ wire, row, limit }: { wire: Wire; row: number; limit: Leaf }) {
  const title = wire.at(`.sessions[${row}].title`);
  const sessionId = wire.at(`.sessions[${row}].session_id`);
  const firstEvent = wire.at(`.sessions[${row}].first_event`);
  const continuable = firstEvent.text === CONTINUABLE_START;
  const titleReason = title.kind === "null" ? nullReasonFor(title.route, title.path) : undefined;
  const titleText = title.kind === "null" ? (titleReason ?? "unknown") : title.text;
  const lastHandler = wire.at(`.sessions[${row}].last_handler`);
  const lastOutcome = wire.at(`.sessions[${row}].last_outcome`);
  const lastCost = wire.at(`.sessions[${row}].last_cost`);
  const lastAt = wire.at(`.sessions[${row}].last_at`);
  const outcomeWord = lastOutcome.kind === "null" ? undefined : humanWord("outcome", lastOutcome.text);

  return (
    <li className={styles.row}>
      {continuable ? (
        <a
          className={styles.title}
          href={resumeHref(sessionId.text, limit.text)}
          data-figure=""
          data-route={title.route}
          data-path={title.path}
        >
          {titleText}
        </a>
      ) : (
        <span className={styles.title} data-figure="" data-route={title.route} data-path={title.path}>
          {titleText}
        </span>
      )}
      <span className={styles.meta}>
        {lastHandler.kind === "null" ? null : (
          <>
            <Value leaf={lastHandler} />
            <Value
              leaf={lastOutcome}
              human={outcomeWord === undefined ? undefined : { text: outcomeWord, exact: lastOutcome.text }}
            />
            <Value leaf={lastCost} />
            <Value leaf={lastAt} human={humanTimestamp(lastAt, now())} />
          </>
        )}
        {continuable ? (
          <a className={styles.resume} href={resumeHref(sessionId.text, limit.text)}>
            {RESUME}
          </a>
        ) : (
          <span className={styles.blocked}>{CANNOT_CONTINUE}</span>
        )}
        <SourceChip label={DETAILS} leaf={sessionId} />
      </span>
    </li>
  );
}

/**
 * The 20 / 50 / 100 choice, the Sessions page's own radios. Choosing pushes the console's URL with
 * every other parameter it carries kept, so choosing a limit never closes the open session.
 */
function LimitControl({ limit }: Props) {
  const router = useRouter();
  const query = useSearchParams();

  function choose(choice: string): void {
    const next = new URLSearchParams(query);
    next.set("limit", choice);
    router.push(`${CONSOLE_PATH}?${next.toString()}`);
  }

  return (
    <fieldset className={styles.limit} data-testid="limit-control">
      <legend className={styles.limitLegend}>rows to ask for</legend>
      {LIMITS.map((choice) => (
        <label key={choice} className={styles.limitChoice}>
          <input
            type="radio"
            name="history-limit"
            value={choice}
            checked={choice === limit.text}
            onChange={() => choose(choice)}
          />
          {choice}
        </label>
      ))}
    </fieldset>
  );
}
