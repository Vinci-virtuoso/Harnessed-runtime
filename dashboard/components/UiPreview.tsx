"use client";

/**
 * Markup an agent produced, run nowhere near the operator's page (FR-127, `D-10`). The `iframe`
 * carries `sandbox=""` exactly (no scripts, no same origin, no forms), `srcdoc` only and **0**
 * `src`: the markup is fetched as text from `lib/outputs.ts`'s body URL and handed to `srcdoc`,
 * because an `src` pointing at the same body would still be a request an unsandboxed browser
 * feature could act on. "View source" switches to `CodeView`; "Open full size" opens the same
 * sandbox in a focus-trapped modal, Esc closes it and focus returns to the button that opened it.
 */
import { type KeyboardEvent, useEffect, useRef, useState } from "react";

import type { Leaf } from "../lib/wire";

import { outputBodyUrl } from "../lib/outputs";
import { CodeView } from "./CodeView";
import styles from "./UiPreview.module.css";

type Props = {
  outputId: Leaf;
  name?: Leaf;
};

const FOCUSABLE = "button, [href], input, select, textarea, [tabindex]";

function focusable(root: HTMLElement | null): HTMLElement[] {
  if (root === null) return [];
  return [...root.querySelectorAll<HTMLElement>(FOCUSABLE)].filter(
    (node) => node.getAttribute("tabindex") !== "-1",
  );
}

export function UiPreview({ outputId, name }: Props) {
  const [markup, setMarkup] = useState<string | undefined>(undefined);
  const [mode, setMode] = useState<"preview" | "source">("preview");
  const [fullSize, setFullSize] = useState(false);
  const openerRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    let cancelled = false;
    fetch(outputBodyUrl(outputId.text), { cache: "no-store" })
      .then((response) => response.text())
      .then((text) => {
        if (!cancelled) setMarkup(text);
      })
      .catch(() => {
        if (!cancelled) setMarkup("");
      });
    return () => {
      cancelled = true;
    };
  }, [outputId.text]);

  if (markup === undefined) {
    return (
      <p className={styles.loading} data-testid="ui-preview-loading">
        Loading preview…
      </p>
    );
  }

  const filename = name === undefined ? outputId.text : name.text;
  const title = `UI preview: ${filename}`;
  const sourceLeaf: Leaf = { kind: "string", text: markup, path: outputId.path, route: outputId.route };

  function closeFullSize(): void {
    setFullSize(false);
    openerRef.current?.focus();
  }

  return (
    <div className={styles.frame} data-testid="ui-preview">
      <div className={styles.toolbar}>
        <span className={styles.name}>{filename}</span>
        <button type="button" onClick={() => setMode(mode === "source" ? "preview" : "source")}>
          {mode === "source" ? "View preview" : "View source"}
        </button>
        <button type="button" ref={openerRef} onClick={() => setFullSize(true)}>
          Open full size
        </button>
      </div>
      {mode === "source" ? (
        <CodeView output={sourceLeaf} name={name} />
      ) : (
        <iframe
          className={styles.iframe}
          data-testid="ui-preview-frame"
          sandbox=""
          srcDoc={markup}
          title={title}
        />
      )}
      {fullSize ? <FullSizeModal markup={markup} title={title} onClose={closeFullSize} /> : null}
    </div>
  );
}

function FullSizeModal({
  markup,
  title,
  onClose,
}: {
  markup: string;
  title: string;
  onClose: () => void;
}) {
  const panel = useRef<HTMLDivElement>(null);

  useEffect(() => {
    focusable(panel.current).at(0)?.focus();
  }, []);

  useEffect(() => {
    const onEscape = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onEscape);
    return () => document.removeEventListener("keydown", onEscape);
  }, [onClose]);

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
        aria-label={title}
        className={styles.modal}
        onKeyDown={onKeyDown}
      >
        <button type="button" onClick={onClose}>
          Close
        </button>
        <iframe className={styles.fullIframe} data-testid="ui-preview-modal-frame" sandbox="" srcDoc={markup} title={title} />
      </div>
    </div>
  );
}
