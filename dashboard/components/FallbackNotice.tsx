"use client";

/**
 * The line above a normal reply when Jev never answered: Groq answered directly, through the v4
 * gate (FR-122, UJ-18's edge case; design.md "The console in v5", decision 59). The router never
 * routed to an agent on a fallback, so the reply beneath this notice is always a Groq chat reply.
 */
import type { Leaf } from "../lib/wire";

import { Value } from "./Value";
import styles from "./FallbackNotice.module.css";

type Props = {
  /** The fallback decision's own `fallback_cause`: "unreachable", "timeout", "refused" or "invalid". */
  cause: Leaf;
};

export function FallbackNotice({ cause }: Props) {
  return (
    <p className={styles.notice} role="status" data-testid="fallback-notice">
      {"The router did not answer ("}
      <Value leaf={cause} />
      {"), so Groq replied directly."}
    </p>
  );
}
