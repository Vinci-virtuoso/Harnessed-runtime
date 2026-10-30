"use client";

/**
 * The Models page's own foot: a collapsed disclosure named "System", holding the v3 health panel
 * unchanged in content (schema version, database path, route count) - moved off the Overview for
 * operator review, still one read, still polled only by `GatewayStatusPill` (FR-129, design.md
 * "SystemSection").
 *
 * Collapsed by default: nothing inside is mounted until the disclosure opens, the same
 * conditional-render pattern `ProviderCard`'s "rotate key" already uses for its own field.
 */
import { type ReactNode, useState } from "react";

import styles from "./SystemSection.module.css";

export const SYSTEM_TITLE = "System";

type Props = {
  children: ReactNode;
};

export function SystemSection({ children }: Props) {
  const [open, setOpen] = useState(false);

  return (
    <section className={styles.section}>
      <button
        type="button"
        className={styles.trigger}
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        {SYSTEM_TITLE}
      </button>
      {open ? <div className={styles.body}>{children}</div> : null}
    </section>
  );
}
