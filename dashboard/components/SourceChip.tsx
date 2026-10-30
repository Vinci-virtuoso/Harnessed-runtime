"use client";

/**
 * The small caption chip of `D-4`'s source-label vocabulary (decision 68).
 *
 * From v5 it is drawn nowhere beside an ordinary figure - `Value` is its own trigger now - and
 * survives only where a label helps the reading: a reply caption, a route line, a cost line
 * (tickets 188, 189). It always sits under a `ResponseSource` or a `PanelFrame`, the same as any
 * other figure: `usePanel` throws with no response to open, which is right for a chip whose whole
 * point is opening one.
 *
 * Its accessible name is "Source: " plus the caption label ("Source: Groq"), never a route or a
 * method (`D-9`); pressing it opens `ProvenanceDrawer` over the response this chip's panel
 * fetched, cited to the one field `leaf` names.
 */
import { useRef, useState } from "react";
import { createPortal } from "react-dom";

import { cliTwinFor } from "../lib/cli-twins";
import type { Leaf } from "../lib/wire";

import { usePanel } from "./PanelFrame";
import { ProvenanceDrawer } from "./ProvenanceDrawer";
import styles from "./SourceChip.module.css";

type Props = {
  /** The caption text (design.md's source-label vocabulary): "Groq", "Jev 96%", "Claude Code". */
  label: string;
  /** The one field this chip's drawer cites. */
  leaf: Leaf;
};

export function SourceChip({ label, leaf }: Props) {
  const { response, sourceLabel } = usePanel();
  const chip = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);

  /** Closing hands focus back to the chip that opened the drawer, wherever it wandered. */
  function close(): void {
    setOpen(false);
    chip.current?.focus();
  }

  return (
    <>
      <button
        ref={chip}
        type="button"
        className={styles.chip}
        aria-label={`Source: ${label}`}
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen(true)}
        // See `Value`'s own note: Enter's default is cancelled so the drawer that mounts on
        // keydown does not also receive the activation that follows it, which would close it.
        onKeyDown={(event) => {
          if (event.key !== "Enter") return;
          event.preventDefault();
          setOpen(true);
        }}
      >
        <span className={styles.route}>{label}</span>
      </button>
      {open
        ? createPortal(
            <ProvenanceDrawer
              response={response}
              sourceLabel={sourceLabel}
              leaf={leaf}
              cliTwin={cliTwinFor(leaf.route)}
              onClose={close}
            />,
            document.body,
          )
        : null}
    </>
  );
}
