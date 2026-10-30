"use client";

/**
 * Where a figure came from, in full (FR-51, `D-4`, `D-9`; decisions 31, 43, 68).
 *
 * From v5 the drawer shows six parts and no endpoint string anywhere in them: **From** (the source
 * label a `Value` was rendered under), **What** (a human field label, the field's own bare name in
 * mono beneath it - never a full jq path, which `D-9` bans as rendered text), **Value** (the exact
 * wire text, verbatim, regardless of any human rendering the page showed), **Updated** (relative,
 * plus `hh:mm:ss (dashboard clock)`), **Answer** (the status word: "ok", "not found", "refused"),
 * then the whole record the figure was read from as a `RecordView` with the cited field marked
 * `aria-current` and highlighted, and finally "Raw access": the CLI `--json` twin, where
 * `lib/cli-twins.ts` names one, with a Copy button. No method, no path, no byte count, no `curl`.
 *
 * It is a modal dialog: `role="dialog"` with `aria-modal`, labelled by its own heading ("Where this
 * came from" - never the route), focus moved inside on open and trapped there, Esc closes, and the
 * opener takes focus back. Under `prefers-reduced-motion: reduce` it opens with no transition.
 */
import { type KeyboardEvent, useEffect, useId, useRef, useState } from "react";

import { clockTime, minutesAgo, now } from "../lib/clock";
import type { RawResponse } from "../lib/fetch-panel";
import { exactReasonFor } from "../lib/null-reasons";
import type { Leaf } from "../lib/wire";

import { RecordView } from "./RecordView";
import styles from "./ProvenanceDrawer.module.css";

/** Every control a focus trap cycles through, minus anything deliberately out of the tab order. */
const FOCUSABLE = "button, [href], input, select, textarea, [tabindex]";

/** The operator's setting, read when the drawer opens because that is when it slides or does not. */
const REDUCE = "(prefers-reduced-motion: reduce)";

const HEADING = "Where this came from";
const RAW_ACCESS = "The terminal shows the raw record:";

/** The one word an absent figure's exact `Value` row shows (`D-2`); never the string `null`. */
const UNKNOWN = "unknown";

type Props = {
  /** The response the figure was read out of, kept whole for this drawer (FR-51). */
  response: RawResponse;
  /** The plain label of who or what produced the figure (`D-4`'s source-label vocabulary). */
  sourceLabel: string;
  /** The one field this drawer opened on. */
  leaf: Leaf;
  /** The CLI command that prints the same payload, where `lib/cli-twins.ts` names one. */
  cliTwin?: string;
  onClose: () => void;
};

export function ProvenanceDrawer({ response, sourceLabel, leaf, cliTwin, onClose }: Props) {
  const id = useId();
  const headingId = `${id}-heading`;
  const panel = useRef<HTMLDivElement>(null);
  // Read once, at open: a drawer that is already sliding does not restart because the setting moved.
  const [motion] = useState(() => (reducedMotion() ? "instant" : "slide"));
  const record: Leaf = { kind: "string", text: response.text, path: "", route: response.url };
  const field = fieldName(leaf.path);
  const exactReason = leaf.kind === "null" ? exactReasonFor(leaf.route, leaf.path) : undefined;

  useEffect(() => {
    focusable(panel.current).at(0)?.focus();
  }, []);

  // Esc is heard on the document, not the panel: it closes wherever the operator's focus went.
  useEffect(() => {
    const onEscape = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onEscape);
    return () => document.removeEventListener("keydown", onEscape);
  }, [onClose]);

  /** The trap: only the two edges are steered, so Tab inside the drawer behaves normally. */
  function onKeyDown(event: KeyboardEvent<HTMLDivElement>): void {
    if (event.key !== "Tab") return;
    const controls = focusable(panel.current);
    const first = controls.at(0);
    const last = controls.at(-1);
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last?.focus();
      return;
    }
    if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first?.focus();
    }
  }

  return (
    <div className={styles.layer}>
      <div className={styles.scrim} aria-hidden="true" />
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby={headingId}
        data-motion={motion}
        className={styles.drawer}
        onKeyDown={onKeyDown}
      >
        <header className={styles.header}>
          <h2 id={headingId} className={styles.heading}>
            {HEADING}
          </h2>
          <button type="button" className={styles.close} onClick={onClose}>
            Close
          </button>
        </header>

        <dl className={styles.facts}>
          <div className={styles.fact}>
            <dt className={styles.term}>From</dt>
            <dd className={styles.value}>{sourceLabel}</dd>
          </div>
          <div className={styles.fact}>
            <dt className={styles.term}>What</dt>
            <dd className={styles.value}>
              {fieldLabel(field)}
              <br />
              <code data-testid="field-name">{field}</code>
            </dd>
          </div>
          <div className={styles.fact}>
            <dt className={styles.term}>Value</dt>
            <dd className={styles.value} data-testid="exact-value">
              {leaf.kind === "null" ? UNKNOWN : leaf.text}
            </dd>
          </div>
          {exactReason === undefined ? null : (
            <div className={styles.fact}>
              <dt className={styles.term}>Exact reason</dt>
              <dd className={styles.value} data-testid="exact-reason">
                {exactReason}
              </dd>
            </div>
          )}
          <div className={styles.fact}>
            <dt className={styles.term}>Updated</dt>
            <dd className={styles.value}>
              {minutesAgo(response.fetchedAt, now())}, {clockTime(response.fetchedAt)} (dashboard
              clock)
            </dd>
          </div>
          <div className={styles.fact}>
            <dt className={styles.term}>Answer</dt>
            <dd className={styles.value} data-testid="answer">
              {answerWord(response.status)}
            </dd>
          </div>
        </dl>

        <section className={styles.section} aria-label="The record this figure was read from">
          <RecordView output={record} citedPath={leaf.path} />
        </section>

        {cliTwin === undefined ? null : (
          <section className={styles.section} aria-label="Raw access">
            <p className={styles.value}>{RAW_ACCESS}</p>
            <div className={styles.command}>
              <code className={styles.code} data-testid="cli-twin">
                {cliTwin}
              </code>
              <button type="button" className={styles.copy} onClick={() => copy(cliTwin)}>
                Copy
              </button>
            </div>
          </section>
        )}
      </div>
    </div>
  );
}

/** "ok", "not found" or "refused": the drawer's Answer word, from the response's own status. */
function answerWord(status: string): string {
  if (status === "200") return "ok";
  if (status === "404") return "not found";
  return "refused";
}

/** The bare field name a jq path ends on - never the dotted path itself (`D-9` bans that as text). */
function fieldName(path: string): string {
  const segments = path.split(".");
  const last = (segments.at(-1) ?? "").replace(/\[\d+\]$/, "");
  return last === "" ? "value" : last;
}

/** A human label from a bare field name: underscores to spaces, one capital to open it. */
function fieldLabel(field: string): string {
  const spaced = field.replaceAll("_", " ");
  if (spaced === "") return "Value";
  return `${spaced.charAt(0).toUpperCase()}${spaced.slice(1)}`;
}

/** Whether the operator asked for no motion. A browser with no `matchMedia` is treated as asking. */
function reducedMotion(): boolean {
  const media = globalThis.matchMedia;
  if (typeof media !== "function") return true;
  return media(REDUCE).matches;
}

function focusable(root: HTMLElement | null): HTMLElement[] {
  if (root === null) return [];
  return [...root.querySelectorAll<HTMLElement>(FOCUSABLE)].filter(
    (node) => node.getAttribute("tabindex") !== "-1",
  );
}

/** Put the line on the clipboard. A browser that refuses simply leaves the text to be selected. */
function copy(line: string): void {
  void globalThis.navigator?.clipboard?.writeText(line);
}
