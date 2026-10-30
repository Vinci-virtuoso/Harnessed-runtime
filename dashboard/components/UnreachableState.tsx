"use client";

/**
 * The full main-area state for a gateway that cannot be reached (design.md, Component inventory).
 * It names the fetch error when there is one and `harnessed serve` as the fix, and draws no
 * host, port or URL (`D-9`, decision 68), and renders nothing remembered (`D-6`).
 *
 * It is **UJ-12's Path A and Path B at once**. A database behind its migrations makes `create_app`
 * raise `SchemaOutOfDateError` and `harnessed serve` never finishes starting, which from a browser
 * is the same silence as no gateway at all: nothing answered at the URL. So this state does not
 * guess which of the two happened - it says where the answer is, in the terminal that ran `serve`.
 * A state that quotes the expected and the on-disk schema version needs the gateway to be up and
 * report the second one, which is `[GAP G-2]`.
 */
import { useEffect, useId, useRef } from "react";

import styles from "./UnreachableState.module.css";

type Props = {
  cause?: string;
};

export function UnreachableState({ cause }: Props) {
  const heading = useRef<HTMLHeadingElement>(null);
  // One id per card: the Overview can show eight at once, and a shared id labels all eight
  // regions with the first heading.
  const headingId = useId();
  useEffect(() => heading.current?.focus(), []);

  return (
    <section className={styles.card} aria-labelledby={headingId}>
      <span className={styles.dot} aria-hidden="true" />
      <h2 id={headingId} ref={heading} tabIndex={-1} className={styles.heading}>
        Gateway unreachable
      </h2>
      <p className={styles.body}>
        The dashboard got no answer from the gateway.
        {cause ? (
          <>
            {" "}
            The request failed with{" "}
            <code className={styles.data} data-testid="unreachable-cause">
              {cause}
            </code>
            .
          </>
        ) : null}
      </p>
      <p className={styles.body}>
        Start the gateway with <code className={styles.command}>harnessed serve</code>, then
        reload this page.
      </p>
      <p className={styles.body}>
        If you started it and nothing is listening, it refused to start - a database behind its
        migrations does that - and the serve terminal names the reason.
      </p>
    </section>
  );
}
