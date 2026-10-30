"use client";

/**
 * One figure with its name (design.md inventory; `D-9`).
 *
 * No box of its own (v6 amendment, FR-163): the card is the `StatGroup` that holds it, so the tile
 * has no border, ground or padding, a sentence-case label and a figure at `type-figure` (a
 * headline) or 14px (a secondary one).
 *
 * The tile wraps exactly one `Value` - that is the whole of its content - so a tile can never
 * become a small dashboard of its own with a second figure quietly derived from the first. The
 * label is the tile's accessible name; the `Value`'s own tooltip is now its description
 * (`D-4`, `D-9`), so no jq path caption is drawn under it any more.
 */
import { useId } from "react";

import type { Leaf } from "../lib/wire";

import { Value } from "./Value";
import styles from "./StatTile.module.css";

type Props = {
  /** The tile's name, and the accessible name of the group it renders. */
  label: string;
  leaf: Leaf;
  /** Why the gateway sent nothing, where it sent nothing (`D-2`). */
  nullReason?: string;
  /** A group's headline figures draw at `type-figure`, its secondary ones at 14px. */
  size?: "headline" | "secondary";
};

export function StatTile({ label, leaf, nullReason, size = "headline" }: Props) {
  const tile = useId();
  const labelId = `${tile}-label`;

  return (
    <div className={`${styles.tile} ${styles[size]}`} role="group" aria-labelledby={labelId}>
      <span id={labelId} className={styles.label}>
        {label}
      </span>
      <span className={styles.figure}>
        <Value leaf={leaf} nullReason={nullReason} />
      </span>
    </div>
  );
}
