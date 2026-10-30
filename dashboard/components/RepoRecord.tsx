"use client";

/**
 * What the repository became under one hop's delegation (FR-145, FR-154, UJ-28; design.md
 * "RepoRecord"): the branch, the served short heads, git's own diff stat verbatim and the served
 * paths - never a count of them made here (G-49). A hop with no served repository draws nothing.
 *
 * "Copy diff command" is `git -C <the task's cwd> diff <head_before> <head_after>` from served
 * values only (V6-3); a delegation whose head did not move but left paths (the served `committed` is
 * "false") reads "Uncommitted changes" and diffs the working tree: `git -C <cwd> diff <head_before>`; where the task's cwd is not known yet it is the same command without `-C`.
 */
import { leafIfSent, rowsOf } from "../lib/rows";
import type { Leaf, Wire } from "../lib/wire";

import { CopyLine } from "./CopyLine";
import { Value } from "./Value";
import styles from "./RepoRecord.module.css";

export const REPO_TITLE = "What the repository became";
export const NOTHING_PUSHED = "Nothing was pushed, merged or deployed. The branch is yours.";

const UNCHANGED = "0";

/** A path or directory with a space in it is quoted for a shell, nothing else is touched. */
function shellWord(text: string): string {
  return text.includes(" ") ? `"${text}"` : text;
}

/** `git -C <cwd> diff <before> <after>`, every part served. */
export function diffCommand(before: Leaf, after: Leaf, cwd: Leaf | undefined): string {
  const where = cwd === undefined || cwd.kind === "null" ? "" : ` -C ${shellWord(cwd.text)}`;
  return `git${where} diff ${before.text} ${after.text}`;
}

/** `git -C <cwd> diff <before>`: the working tree against a head that did not move. */
export function workingTreeCommand(before: Leaf, cwd: Leaf | undefined): string {
  const where = cwd === undefined || cwd.kind === "null" ? "" : ` -C ${shellWord(cwd.text)}`;
  return `git${where} diff ${before.text}`;
}

type Props = {
  /** One hop of a message, scoped to it (decision 42). */
  hop: Wire;
  /** The task's served working directory, once the task's own read has landed. */
  cwd?: Leaf;
};

export function RepoRecord({ hop, cwd }: Props) {
  const branch = leafIfSent(hop, ".repository.branch");
  if (branch === undefined) return null;
  const changed = hop.at(".repository.changed_count");
  const before = hop.at(".repository.head_before");
  const after = hop.at(".repository.head_after");
  const diffStat = hop.at(".repository.diff_stat");
  const paths = rowsOf(hop, (row) => `.repository.changed_paths[${row}]`);
  const unchanged = changed.text === UNCHANGED;
  const uncommitted = !unchanged && hop.at(".repository.committed").text === "false";
  const headline = unchanged ? "No files changed on " : uncommitted ? "Uncommitted changes on " : "Changed files on ";

  return (
    <section className={styles.card} aria-label={REPO_TITLE} data-testid="repo-record">
      <p className={styles.headline}>
        {headline}
        <span className={styles.branch}>
          <Value leaf={branch} />
        </span>
      </p>
      <p className={styles.heads}>
        {uncommitted ? (
          <>
            {"At "}
            <Value leaf={hop.at(".repository.head_after_short")} />
          </>
        ) : (
          <>
            <Value leaf={hop.at(".repository.head_before_short")} /> to{" "}
            <Value leaf={hop.at(".repository.head_after_short")} />
          </>
        )}
      </p>
      {unchanged || diffStat.text === "" ? null : (
        <p className={styles.stat}>
          <Value leaf={diffStat} />
        </p>
      )}
      {unchanged ? null : (
        <ul className={styles.paths}>
          {paths.map((row) => (
            <li key={row} className={styles.path}>
              <Value leaf={hop.at(`.repository.changed_paths[${row}]`)} />
            </li>
          ))}
        </ul>
      )}
      <p className={styles.closing}>{NOTHING_PUSHED}</p>
      <CopyLine line={uncommitted ? workingTreeCommand(before, cwd) : diffCommand(before, after, cwd)} label="Copy diff command" testId="diff-command" />
    </section>
  );
}
