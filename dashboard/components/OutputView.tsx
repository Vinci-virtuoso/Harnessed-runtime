"use client";

/**
 * One output, exactly one presenter (FR-127, `D-10`, decision 65; MUST rule 32).
 *
 * The presenter is chosen from `kind.text` - the served `output_kind` - and nothing else: never a
 * parse of `output`'s own characters, never a file-extension switch on `name`. A kind this table
 * does not name, or a kind whose body this render has nothing to show (no inline `output`, no
 * `outputId` to read one by), falls to `FileCard`, which never guesses a presentation either.
 */
import type { ReactNode } from "react";

import type { Leaf } from "../lib/wire";

import { CodeView } from "./CodeView";
import { DiagramView } from "./DiagramView";
import { FileCard } from "./FileCard";
import { ImageView } from "./ImageView";
import styles from "./OutputView.module.css";
import { RecordView } from "./RecordView";
import { TextView } from "./TextView";
import { UiPreview } from "./UiPreview";

/** The six served words FR-127 names; anything else is the media type verbatim, and `FileCard`. */
export const KIND_TEXT = "text";
export const KIND_UI = "ui";
export const KIND_IMAGE = "image";
export const KIND_DIAGRAM = "diagram";
export const KIND_CODE = "code";
export const KIND_RECORD = "record";

export type Props = {
  /** The served `output_kind`. */
  kind: Leaf;
  /** The output's own text, where the wire carries it inline; `null` for a kind read by id. */
  output: Leaf | null;
  /** The stored row's id, for a kind whose body `lib/outputs.ts` fetches or links to. */
  outputId?: Leaf;
  /** The filename, where one was served. */
  name?: Leaf;
  /** The served size, shown by `FileCard`. */
  size?: Leaf;
};

export function OutputView({ kind, output, outputId, name, size }: Props) {
  const fallback = <FileCard kind={kind} output={output} outputId={outputId} name={name} size={size} />;

  return (
    <div className={styles.output} data-testid="output-view" data-kind={kind.text}>
      {present(kind.text, { output, outputId, name, fallback })}
    </div>
  );
}

function present(
  kindWord: string,
  args: { output: Leaf | null; outputId: Leaf | undefined; name: Leaf | undefined; fallback: ReactNode },
): ReactNode {
  const { output, outputId, name, fallback } = args;
  switch (kindWord) {
    case KIND_TEXT:
      return output === null ? fallback : <TextView output={output} />;
    case KIND_CODE:
      return output === null ? fallback : <CodeView output={output} name={name} />;
    case KIND_RECORD:
      return output === null ? fallback : <RecordView output={output} />;
    case KIND_UI:
      return outputId === undefined ? fallback : <UiPreview outputId={outputId} name={name} />;
    case KIND_IMAGE:
      return outputId === undefined ? fallback : <ImageView outputId={outputId} name={name} />;
    case KIND_DIAGRAM:
      return outputId === undefined ? fallback : <DiagramView outputId={outputId} name={name} />;
    default:
      return fallback;
  }
}
