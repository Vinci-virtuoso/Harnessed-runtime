"use client";

/**
 * The fallback presenter: a kind `OutputView`'s table does not name, or one with no body to show
 * it (FR-127, `D-10`). It never guesses a presentation - it names the kind word exactly as the
 * wire spelled it, the served size, and the CLI line that writes the file to disk, static copy
 * `lib/cli-twins.ts` already holds for `/v1/outputs/{id}` (MUST rule 25: no id is assembled here).
 */
import type { Leaf } from "../lib/wire";

import { cliTwinFor } from "../lib/cli-twins";
import styles from "./FileCard.module.css";

type Props = {
  kind: Leaf;
  output?: Leaf | null;
  outputId?: Leaf;
  name?: Leaf;
  size?: Leaf;
};

/** The static command every output body's twin resolves to; the `{id}` wildcard matches anything. */
const OUTPUT_TWIN_ROUTE = "/v1/outputs/{id}";

export function FileCard({ kind, outputId, name, size }: Props) {
  const command = outputId === undefined ? undefined : cliTwinFor(OUTPUT_TWIN_ROUTE);

  return (
    <div className={styles.card} data-testid="file-card">
      <span className={styles.name}>{name === undefined ? kind.text : name.text}</span>
      <span className={styles.kind} data-testid="file-card-kind">
        {kind.text}
      </span>
      {size === undefined ? null : <span className={styles.note}>{size.text}</span>}
      <span className={styles.note}>no presentation for this kind yet</span>
      {command === undefined ? null : (
        <code className={styles.command} data-testid="file-card-cli">
          {command}
        </code>
      )}
    </div>
  );
}
