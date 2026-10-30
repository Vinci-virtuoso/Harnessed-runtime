"use client";

/**
 * A raster or SVG image (FR-127, `D-10`). Loaded as an `<img>` from `lib/outputs.ts`'s body URL -
 * SVG included, **never** inlined as raw markup (MUST rule and G-35). `alt` is the served caption
 * where the caller has one, else the filename, never empty (FR-127).
 */
import { useState } from "react";

import type { Leaf } from "../lib/wire";

import { outputBodyUrl } from "../lib/outputs";
import styles from "./ImageView.module.css";

type Props = {
  outputId: Leaf;
  name?: Leaf;
  caption?: Leaf;
};

export function ImageView({ outputId, name, caption }: Props) {
  const [unreadable, setUnreadable] = useState(false);
  const alt = caption?.text ?? name?.text ?? outputId.text;

  if (unreadable) {
    return (
      <p className={styles.unreadable} data-testid="image-unreadable">
        the image could not be read
      </p>
    );
  }

  return (
    <img
      className={styles.image}
      data-testid="image-view"
      src={outputBodyUrl(outputId.text)}
      alt={alt}
      onError={() => setUnreadable(true)}
    />
  );
}
