"use client";

/**
 * The Overview's "Needs you": `GET /v1/tasks?state=needs_human`, rows in the order the store served
 * (longest waiting first, FR-153, G-59), and nothing counted here (MUST rule 21, `D-1`).
 *
 * Each row's pause sentence is built from the **served** pause fields only: a permission pause
 * names the agent, the tool kind's words and the agent's own description; an uncertain route names
 * the agent, its served confidence percent and its served floor percent. "since" is the served
 * `paused_at`, humanized. The `Open` link carries the task's own session id verbatim (MUST rule 25).
 *
 * The list is a `ul` inside the panel's labelled `section` and is **not** a live region, because a
 * live region must not hold links (G-55). A separate visually hidden polite `role="status"` region
 * says the row count when it changes, and holds nothing else.
 */
import { useRef, useState } from "react";

import { now } from "../lib/clock";
import { humanWord } from "../lib/human-words";
import { humanTimestamp } from "../lib/humanize";
import { rowsOf } from "../lib/rows";
import type { PanelResult } from "../lib/fetch-panel";
import type { Leaf, Wire } from "../lib/wire";

import { PanelFrame } from "./PanelFrame";
import { Value } from "./Value";
import styles from "./NeedsYouList.module.css";

export const NEEDS_YOU_TITLE = "Needs you";
export const NEEDS_YOU_ROUTE = "/v1/tasks?state=needs_human";
export const NOTHING_NEEDS_YOU = "Nothing needs you.";

/** The announced sentence for a row count that changed. */
export function countSentence(count: number): string {
  if (count === 0) return NOTHING_NEEDS_YOU;
  return count === 1 ? "1 task needs you" : `${count} tasks need you`;
}

function lowerFirst(text: string): string {
  return `${text.charAt(0).toLowerCase()}${text.slice(1)}`;
}

function served(leaf: Leaf): string | undefined {
  return leaf.kind === "null" ? undefined : leaf.text;
}

/** The pause in words, from the served pause fields and nothing else. */
export function pauseSentence(wire: Wire, row: number): string {
  const at = (field: string): string | undefined => served(wire.at(`.tasks[${row}].${field}`));
  const reason = at("pause_reason");
  if (reason === "permission") {
    const agent = at("pause_agent") ?? "An agent";
    const kind = at("pause_tool_kind");
    const words = kind === undefined ? undefined : humanWord("tool_kind", kind);
    const head = `${agent} asks to ${words === undefined ? "do something" : lowerFirst(words)}`;
    const description = at("pause_description");
    return description === undefined ? head : `${head}: ${description}`;
  }
  if (reason === "uncertain") {
    const route = at("pause_route") ?? "an agent";
    const confidence = at("pause_confidence_percent");
    const floor = at("pause_floor_percent");
    const detail =
      confidence === undefined || floor === undefined
        ? route
        : `${route} at ${confidence}, below its ${floor} floor`;
    return `The router was not sure: ${detail}`;
  }
  return "Waiting for you";
}

type Props = {
  /** A refetch signal from the Overview's watched stream or the shell's Refresh. */
  refresh?: object;
};

export function NeedsYouList({ refresh }: Props) {
  const previous = useRef<number | undefined>(undefined);
  const [announced, setAnnounced] = useState("");

  const settled = (result: PanelResult) => {
    if (result.state !== "ok") return;
    const count = rowsOf(result.wire, (index) => `.tasks[${index}].session_id`).length;
    if (previous.current !== undefined && previous.current !== count) {
      setAnnounced(countSentence(count));
    }
    previous.current = count;
  };

  return (
    <>
      <PanelFrame
        route={NEEDS_YOU_ROUTE}
        title={NEEDS_YOU_TITLE}
        sourceLabel="Tasks: now"
        refresh={refresh}
        onSettled={settled}
        skeletonRows={2}
      >
        {(wire) => {
          const rows = rowsOf(wire, (index) => `.tasks[${index}].session_id`);
          if (rows.length === 0) return <p className={styles.nothing}>{NOTHING_NEEDS_YOU}</p>;
          return (
            <ul className={styles.list} data-testid="needs-you-list">
              {rows.map((row) => (
                <NeedsYouRow key={row} wire={wire} row={row} />
              ))}
            </ul>
          );
        }}
      </PanelFrame>
      <p className={styles.hidden} role="status" aria-live="polite" data-testid="needs-you-count">
        {announced}
      </p>
    </>
  );
}

function NeedsYouRow({ wire, row }: { wire: Wire; row: number }) {
  const text = wire.at(`.tasks[${row}].text`);
  const sessionId = wire.at(`.tasks[${row}].session_id`);
  const pausedAt = wire.at(`.tasks[${row}].paused_at`);
  const sentence = pauseSentence(wire, row);
  const title = text.kind === "null" ? "unknown" : text.text;

  return (
    <li className={styles.row}>
      <div className={styles.what}>
        <span className={styles.title} data-figure="" data-route={text.route} data-path={text.path}>
          {title}
        </span>
        <span className={styles.pause}>{sentence}</span>
      </div>
      <span className={styles.since}>
        {pausedAt.kind === "null" ? null : (
          <>
            since <Value leaf={pausedAt} human={humanTimestamp(pausedAt, now())} />
          </>
        )}
      </span>
      <a
        className={styles.open}
        href={`/console?session=${encodeURIComponent(sessionId.text)}`}
        aria-label={`Open ${title}: ${sentence}`}
      >
        Open
      </a>
    </li>
  );
}
