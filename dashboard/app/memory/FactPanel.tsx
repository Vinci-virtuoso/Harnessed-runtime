"use client";

/**
 * One fact, read back field by field (FR-58, UJ-10).
 *
 * Every figure is a leaf of this panel's own response and carries the path it was read from, so a
 * fact's text is one Tab from `/memory/facts/{mem_id} -> .text` (`D-4`).
 *
 * **`status` is shown for every fact, not only for an archived one.** A fact reached by id may be
 * active or archived - the search serves only the active ones, and this route serves both - so the
 * word the gateway sent is always on screen. Leaving it off an active fact would make its absence
 * the signal, which is a fact about this page rather than about the store.
 *
 * **The text is rendered exactly as stored.** Where `memory/redact.py` replaced a secret on write,
 * what it wrote is what the ledger holds and what this shows: the dashboard adds no redaction pass
 * and must not weaken one (PRD, Security), so nothing here re-masks, escapes or reflows a
 * character (decision 31).
 *
 * **An empty collection is a sentence.** A fact with no tag and no link is not a fact with two
 * blank fields, so the panel says which it is rather than drawing an empty row (`D-3`).
 */
import { PanelFrame } from "../../components/PanelFrame";
import { Value } from "../../components/Value";
import { now } from "../../lib/clock";
import { humanAgo, humanFromConsolidation } from "../../lib/humanize";
import type { Wire } from "../../lib/wire";

import { rowsOf } from "../../lib/rows";
import { factRoute } from "./search";
import styles from "./memory.module.css";

/** The panel's name, so a test and a screen reader find it by the same words. */
export const PANEL_TITLE = "Fact";

/** What the panel says where the fact carries no tag at all. */
export const NO_TAGS = "the fact carries no tag";

/** What the panel says where the fact links to nothing. */
export const NO_LINKS = "the fact links to no other fact";

/** The fields a fact is read by, in the order this panel reads them. */
const FIELDS = ["mem_id", "status", "source", "created_at"];

type Props = {
  /** The id the URL carried, passed to the gateway exactly as it arrived. */
  memId: string;
};

export function FactPanel({ memId }: Props) {
  const route = factRoute(memId);

  return (
    <PanelFrame route={route} title={PANEL_TITLE} sourceLabel="Ledger: all time">
      {(wire) => {
        const words = humanFromConsolidation(wire.at(".source"), humanAgo(wire.at(".created_at"), now()));
        return (
        <div className={styles.body}>
          <div className={styles.identity}>
            {FIELDS.map((field) => (
              <span key={field} className={styles.field}>
                <span className={styles.fieldName}>{field}</span>
                <Value leaf={wire.at(`.${field}`)} human={field === "source" ? words : undefined} />
              </span>
            ))}
            {words === undefined ? null : (
              <span className={styles.field} data-testid="fact-source-exact">
                <span className={styles.fieldName}>exact source</span>
                <Value leaf={wire.at(".source")} />
              </span>
            )}
          </div>
          <p className={styles.episodeText}>
            <Value leaf={wire.at(".text")} />
          </p>
          <Collection wire={wire} field="tags" empty={NO_TAGS} />
          <Collection wire={wire} field="links" empty={NO_LINKS} />
        </div>
        );
      }}
    </PanelFrame>
  );
}

/**
 * One of the fact's two collections, in the response's own order. The walk asks the wire for the
 * next item until there is none, so the length is the response's and never an arithmetic of this
 * page's (decision 32).
 */
function Collection({ wire, field, empty }: { wire: Wire; field: string; empty: string }) {
  const rows = rowsOf(wire, (index) => `.${field}[${index}]`);
  if (rows.length === 0) {
    return (
      <p className={styles.note} data-testid={`fact-${field}-empty`}>
        {empty}
      </p>
    );
  }

  return (
    <ul className={styles.chips} aria-label={field}>
      {rows.map((row) => (
        <li key={row} className={styles.chip}>
          <Value leaf={wire.at(`.${field}[${row}]`)} />
        </li>
      ))}
    </ul>
  );
}
