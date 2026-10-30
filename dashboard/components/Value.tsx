"use client";

/**
 * One figure, and its own trigger (`D-1`, `D-4`, `D-9`; MUST rule 21; decision 68).
 *
 * It takes a `Leaf` and never a numeric value, and renders `leaf.text` verbatim - or, where the
 * caller hands a human rendering (`lib/humanize.ts`), that rendering's own words - nothing is
 * rounded, padded or turned into a percentage. `data-figure`, `data-route` and `data-path` are the
 * provenance hooks the fidelity, provenance and page-walk tests read, never rendered into text.
 *
 * **The figure is itself the trigger.** Inside a `PanelFrame` (or a `ResponseSource`) it is a
 * `button`: its tooltip reads "<source label>, updated <relative time>" and, for a human
 * rendering, the exact wire text too; click or Enter opens `ProvenanceDrawer` over the response
 * this figure was read out of. Outside one there is no response to open and no source to name, so
 * it renders plainly, with its route and path kept as the same non-rendered hooks.
 *
 * **A `null` is not a value** (`D-2`). Where the gateway sent one, the figure is the `unknown`
 * treatment - the word, italic, in a dashed chip over the hatch - and never `0`, `-` or a blank,
 * because those are measurements and this is the absence of one. The reason sits beside it,
 * static copy from `lib/null-reasons.ts` keyed on the same route and path (rendered for a null
 * leaf only: beside a value it would be a false statement, `D-3`), and the figure
 * announces "unknown, " and that reason.
 */
import { useRef, useState } from "react";
import { createPortal } from "react-dom";

import { minutesAgo, now } from "../lib/clock";
import { cliTwinFor } from "../lib/cli-twins";
import type { Human } from "../lib/humanize";
import type { Leaf } from "../lib/wire";

import { type PanelContextValue, usePanel } from "./PanelFrame";
import { ProvenanceDrawer } from "./ProvenanceDrawer";
import styles from "./Value.module.css";

/** The one word an absent figure is allowed to be. */
const UNKNOWN = "unknown";

type Props = {
  leaf: Leaf;
  /** Why the gateway sent nothing, taken verbatim from `lib/null-reasons.ts`. */
  nullReason?: string;
  /** A human rendering of the leaf (`lib/humanize.ts`); the exact wire text always joins the tooltip. */
  human?: Human;
  /** `-1` takes the figure out of the Tab order where its row is already one stop (decision 103); it stays clickable. */
  tabIndex?: -1;
};

export function Value({ leaf, nullReason, human, tabIndex }: Props) {
  const panel = useOptionalPanel();
  const unknown = leaf.kind === "null";
  const displayed = human === undefined ? (unknown ? UNKNOWN : leaf.text) : human.text;
  const [open, setOpen] = useState(false);
  const button = useRef<HTMLButtonElement>(null);

  const figureProps = {
    "data-figure": "",
    "data-route": leaf.route,
    "data-path": leaf.path,
    "aria-label": unknown ? announced(nullReason) : undefined,
  } as const;

  function close(): void {
    setOpen(false);
    button.current?.focus();
  }

  if (panel === undefined) {
    return (
      <span className={styles.group}>
        <span className={unknown ? styles.unknown : styles.figure} {...figureProps}>
          {displayed}
        </span>
        {unknown && nullReason !== undefined ? <span className={styles.reason}>{nullReason}</span> : null}
      </span>
    );
  }

  const title =
    human === undefined
      ? tooltip(panel)
      : `${tooltip(panel)}, ${human.exact}`;

  return (
    <span className={styles.group}>
      <button
        ref={button}
        type="button"
        className={unknown ? styles.unknown : styles.figure}
        {...figureProps}
        title={title}
        tabIndex={tabIndex}
        onClick={() => setOpen(true)}
        onKeyDown={(event) => {
          if (event.key !== "Enter") return;
          // Cancel the button's own Enter activation: mounting the drawer moves focus to its
          // `Close` control before the click that follows Enter would land, which would close it
          // in the same keystroke otherwise (the same defect SourceChip's precedent found).
          event.preventDefault();
          setOpen(true);
        }}
      >
        {displayed}
      </button>
      {unknown && nullReason !== undefined ? <span className={styles.reason}>{nullReason}</span> : null}
      {open
        ? createPortal(
            <ProvenanceDrawer
              response={panel.response}
              sourceLabel={panel.sourceLabel}
              leaf={leaf}
              cliTwin={cliTwinFor(leaf.route)}
              onClose={close}
            />,
            document.body,
          )
        : null}
    </span>
  );
}

/** "<source label>, updated <relative time>" - `D-4`'s tooltip, every word already `at`'s own. */
function tooltip(panel: PanelContextValue): string {
  return `${panel.sourceLabel}, updated ${minutesAgo(panel.response.fetchedAt, now())}`;
}

/**
 * The surrounding panel where there is one. `usePanel` throws outside a `PanelFrame`, which is
 * right for the drawer trigger - a button with no response to open is a button that lies - but
 * wrong for `Value`, which is a faithful figure with or without a panel around it.
 */
function useOptionalPanel(): PanelContextValue | undefined {
  try {
    return usePanel();
  } catch {
    return undefined;
  }
}

/** What a screen reader hears on an unknown figure: the word, then the reason where there is one. */
function announced(nullReason: string | undefined): string {
  return nullReason === undefined ? UNKNOWN : `${UNKNOWN}, ${nullReason}`;
}
