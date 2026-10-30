"use client";

/**
 * The one field the Memory pages let an operator type into (FR-58, FR-60; `[GAP G-17]`).
 *
 * There is no listing of episodes - the gateway serves them for one session id and for nothing
 * else - so a session's episodes are reached by pasting the id a run printed. `IdLookup` sends what
 * was typed exactly as typed and never asks whether it exists; the 404 belongs to the episodes
 * page, where it arrives as `AbsentState` quoting the gateway's own message and the sentence that
 * says what a 404 means on that route.
 *
 * It sits on every memory page, because a session id is as likely to be in hand while reading a
 * fact as while reading the procedure set, and it is a client component because `hrefFor` is a
 * function and a function cannot cross the server boundary.
 */
import { IdLookup } from "../../components/IdLookup";

import { episodeHref } from "./search";
import styles from "./memory.module.css";

/** The section's accessible name, so a test and a screen reader find it by the same words. */
export const LOOKUP_TITLE = "Open a session's episodes";

/** Where the id this field wants comes from, since no listing offers one (`[GAP G-17]`). */
export const LOOKUP_NOTE = "there is no listing of episodes; paste the session id of a run";

export function EpisodeLookup() {
  return (
    <section className={styles.lookup} aria-label={LOOKUP_TITLE}>
      <p className={styles.note} data-testid="lookup-note">
        {LOOKUP_NOTE}
      </p>
      <IdLookup kind="session id" hrefFor={episodeHref} />
    </section>
  );
}
