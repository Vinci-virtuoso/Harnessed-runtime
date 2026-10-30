"use client";

/**
 * A delegation Harnessed refused because the working tree had uncommitted changes, in the reply's
 * place (FR-146, FR-154, UJ-28; design.md "DirtyTreeRefusal"). It is `ErrorState`'s geometry with
 * the served dirty paths and the one command that hands the tree over as it is. The paths come from
 * the refusal's ledger row; the directory from the task's own read, when it has landed.
 */
import { rowsOf } from "../lib/rows";
import type { Leaf, Wire } from "../lib/wire";

import { CopyLine } from "./CopyLine";
import { Value } from "./Value";
import styles from "./DirtyTreeRefusal.module.css";

import { humanWord } from "../lib/human-words";

/** The title when no agent is served for the refusal: the agent is not named, as before. */
export const DIRTY_TITLE_FALLBACK = "The agent did not start";

/** "{agent} did not start": the agent's display name where the words table has one, else the served name. */
export function dirtyTitle(agent: Leaf | undefined): string {
  if (agent === undefined || agent.kind === "null") return DIRTY_TITLE_FALLBACK;
  return `${humanWord("agent", agent.text) ?? agent.text} did not start`;
}

type Props = {
  /** The refused hop, scoped to it (decision 42). */
  hop: Wire;
  /** The task whose message was refused, for the copy line; absent outside a task. */
  taskId?: Leaf;
  /** The task's served working directory, once its read has landed. */
  cwd?: Leaf;
  /** The agent that was refused, as served (the task's entry or the routed agent). */
  agent?: Leaf;
};

export function DirtyTreeRefusal({ hop, taskId, cwd, agent }: Props) {
  const paths = rowsOf(hop, (row) => `.repository.dirty_paths[${row}]`);
  const command = taskId === undefined ? undefined : `harnessed task run ${taskId.text} --allow-dirty`;
  const where = cwd === undefined || cwd.kind === "null" ? "The working tree" : `The working tree at ${cwd.text}`;
  return (
    <div className={styles.refusal} role="alert" data-testid="dirty-tree-refusal">
      <p className={styles.title}>{dirtyTitle(agent)}</p>
      <p className={styles.body}>{`${where} has uncommitted changes, so Harnessed did not hand it over.`}</p>
      <ul className={styles.paths}>
        {paths.map((row) => (
          <li key={row} className={styles.path}>
            <Value leaf={hop.at(`.repository.dirty_paths[${row}]`)} />
          </li>
        ))}
      </ul>
      {command === undefined ? null : (
        <>
          <p className={styles.body}>Commit or stash them, then run the task again. To hand it over as it is:</p>
          <CopyLine line={command} label="Copy" testId="allow-dirty-command" />
        </>
      )}
    </div>
  );
}
