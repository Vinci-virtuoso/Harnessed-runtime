"use client";

/**
 * An architecture or design diagram (FR-127, `D-10`). SVG renders as an `<img>`, exactly as
 * `ImageView`; Mermaid and Excalidraw source is not SVG, so the `<img>` fails to load it and this
 * falls back to its own source in `CodeView` with a line saying why (G-34: no renderer is bundled,
 * decision 68). The fallback text is fetched, never sniffed, from the same output body a
 * successful `<img>` would have shown.
 */
import { useEffect, useState } from "react";

import type { Leaf } from "../lib/wire";

import { outputBodyUrl } from "../lib/outputs";
import { CodeView } from "./CodeView";
import styles from "./DiagramView.module.css";

type Props = {
  outputId: Leaf;
  name?: Leaf;
  caption?: Leaf;
};

export function DiagramView({ outputId, name, caption }: Props) {
  const [failed, setFailed] = useState(false);
  const [source, setSource] = useState<string | undefined>(undefined);

  useEffect(() => {
    if (!failed) return undefined;
    let cancelled = false;
    fetch(outputBodyUrl(outputId.text), { cache: "no-store" })
      .then((response) => response.text())
      .then((text) => {
        if (!cancelled) setSource(text);
      })
      .catch(() => {
        if (!cancelled) setSource("");
      });
    return () => {
      cancelled = true;
    };
  }, [failed, outputId.text]);

  if (failed) {
    if (source === undefined) return null;
    const sourceLeaf: Leaf = { kind: "string", text: source, path: outputId.path, route: outputId.route };
    return (
      <div data-testid="diagram-source">
        <p className={styles.note}>Shown as source: no diagram renderer is bundled yet (G-34).</p>
        <CodeView output={sourceLeaf} name={name} />
      </div>
    );
  }

  const alt = caption?.text ?? name?.text ?? outputId.text;
  return (
    <img
      role="img"
      className={styles.image}
      data-testid="diagram-view"
      src={outputBodyUrl(outputId.text)}
      alt={alt}
      onError={() => setFailed(true)}
    />
  );
}
