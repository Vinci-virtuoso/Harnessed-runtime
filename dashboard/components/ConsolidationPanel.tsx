"use client";

/**
 * Memory's "Consolidation": `GET /v1/memory/consolidation`, the batches that turn episodes into
 * facts, newest first as served (FR-155, FR-152; `ux/design.md`'s `ConsolidationPanel`, `V6-5`).
 *
 * Every figure is a `Value` of the response's own leaf and every count word is chosen from the
 * served string `"1"` (a lookup, never arithmetic: `D-1`). A failed batch says it wrote nothing
 * and that its episodes wait; an empty list says when the next batch runs and offers the real
 * command. Each batch with facts expands (`aria-expanded`) to the facts it wrote, each with an
 * `Open` link to the Facts section. The newest batch's own leaves are lent to the diagram's second
 * status line (decision 42).
 */
import { now } from "../lib/clock";
import { humanWord } from "../lib/human-words";
import { humanAgo, pluralOf } from "../lib/humanize";
import { nullReasonFor } from "../lib/null-reasons";
import { leafIfSent, rowsOf } from "../lib/rows";
import type { Wire } from "../lib/wire";
import { factHref } from "../app/memory/search";

import { CopyLine } from "./CopyLine";
import { Lend, type Lender } from "./diagram/Lend";
import { PanelFrame } from "./PanelFrame";
import { Value } from "./Value";
import { useState } from "react";
import styles from "./ConsolidationPanel.module.css";

export const CONSOLIDATION_TITLE = "Consolidation";
export const CONSOLIDATION_ROUTE = "/v1/memory/consolidation";
export const CONSOLIDATE_COMMAND = "harnessed memory consolidate";

type Props = {
  /** The page's diagram takes the newest batch's leaves back through this (decision 42). */
  lend: Lender;
  /** A fresh token when the watched stream carried `memory.consolidation.finished`. */
  refresh?: object;
};

export function ConsolidationPanel({ lend, refresh }: Props) {
  return (
    <PanelFrame
      route={CONSOLIDATION_ROUTE}
      title={CONSOLIDATION_TITLE}
      sourceLabel="Ledger: all time"
      refresh={refresh}
    >
      {(wire) => {
        const rows = rowsOf(wire, (index) => `.batches[${index}].batch_id`);
        const after = wire.at(".consolidate_after");
        const waiting = wire.at(".unconsolidated");
        return (
          <div className={styles.body}>
            <p className={styles.line} data-testid="consolidation-line">
              Turns past sessions into facts after every <Value leaf={after} /> new{" "}
              {pluralOf(after, "episode")}. <Value leaf={waiting} /> so far.
            </p>
            {rows.length === 0 ? (
              <div className={styles.empty} data-testid="consolidation-empty">
                <p className={styles.line}>
                  No consolidation has run yet. It runs after <Value leaf={after} /> new{" "}
                  {pluralOf(after, "episode")}, or by hand:
                </p>
                <CopyLine line={CONSOLIDATE_COMMAND} label="Copy" testId="consolidation-command" />
              </div>
            ) : (
              <ul className={styles.list} data-testid="consolidation-list">
                <Lend name="consolidation facts" leaf={wire.at(".batches[0].facts_written")} to={lend} />
                <Lend name="consolidation episodes" leaf={wire.at(".batches[0].episode_count")} to={lend} />
                <Lend name="consolidation at" leaf={wire.at(".batches[0].finished_at")} to={lend} />
                {rows.map((row) => (
                  <Batch key={row} wire={wire} row={row} />
                ))}
              </ul>
            )}
          </div>
        );
      }}
    </PanelFrame>
  );
}

function Batch({ wire, row }: { wire: Wire; row: number }) {
  const [open, setOpen] = useState(false);
  const at = (field: string) => wire.at(`.batches[${row}].${field}`);
  const status = at("status");
  const failed = status.text === "failed";
  const facts = at("facts_written");
  const episodes = at("episode_count");
  const finished = at("finished_at");
  const cost = at("cost");
  const hasFacts = leafIfSent(wire, `.batches[${row}].facts[0].mem_id`) !== undefined;
  const statusWord = humanWord("consolidation_status", status.text);
  const factRows = rowsOf(wire, (index) => `.batches[${row}].facts[${index}].mem_id`);

  return (
    <li className={styles.row} data-testid="consolidation-row">
      <p className={styles.headline}>
        {failed ? (
          <>
            Wrote nothing. The <Value leaf={episodes} /> {pluralOf(episodes, "episode")}{" "}
            {episodes.text === "1" ? "waits" : "wait"} for the next batch.
          </>
        ) : (
          <>
            <Value leaf={facts} /> {pluralOf(facts, "fact")} from <Value leaf={episodes} />{" "}
            {pluralOf(episodes, "episode")}
          </>
        )}
      </p>
      <p className={styles.line} data-testid="consolidation-counts">
        <Value leaf={facts} /> written,{" "}
        <Value leaf={at("facts_skipped")} nullReason={nullReasonFor(wire.route, ".batches[].facts_skipped")} /> skipped,{" "}
        <Value leaf={at("facts_archived")} /> archived
      </p>
      <p className={styles.meta}>
        <span className={failed ? styles.failed : undefined}>
          <Value leaf={status} human={statusWord === undefined ? undefined : { text: statusWord, exact: status.text }} />
        </span>
        <Value leaf={at("model")} />
        <Value leaf={cost} nullReason={nullReasonFor(wire.route, ".batches[].cost")} />
        <span className={styles.time}>
          <Value leaf={finished} human={humanAgo(finished, now())} />
        </span>
        {hasFacts ? (
          <button
            type="button"
            className={styles.toggle}
            aria-expanded={open}
            onClick={() => setOpen(!open)}
          >
            {open ? "Hide facts" : "Show facts"}
          </button>
        ) : null}
      </p>
      {open ? (
        <ul className={styles.facts}>
          {factRows.map((fact) => {
            const memId = wire.at(`.batches[${row}].facts[${fact}].mem_id`);
            return (
              <li key={fact} className={styles.fact}>
                <Value leaf={wire.at(`.batches[${row}].facts[${fact}].text`)} />
                <a className={styles.open} href={factHref(memId.text)}>
                  Open
                </a>
              </li>
            );
          })}
        </ul>
      ) : null}
    </li>
  );
}
