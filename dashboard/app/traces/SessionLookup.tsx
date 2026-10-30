"use client";

/**
 * The one field `/sessions` lets an operator type into (FR-59, FR-60; UJ-11).
 *
 * The lookup sits beside the listing rather than instead of it. The listing is capped, so the
 * session an operator has in hand - the id `harnessed eval run smoke --json` just printed - may
 * be below the cap he asked for, and pasting it is the one way to reach it without widening the
 * limit. `IdLookup` sends what was typed exactly as typed and never asks whether it exists; the
 * 404 belongs to the session's own page, where it arrives as `AbsentState` quoting the gateway's
 * own message (MUST rule 25).
 *
 * It is a client component because `hrefFor` is a function, and a function cannot cross the
 * server boundary: a server page handing `sessionHref` straight to `IdLookup` renders in a test
 * and throws in the browser, which is the one defect only the built app can show.
 */
import { IdLookup } from "../../components/IdLookup";

import { sessionHref } from "./limit";
import styles from "./sessions.module.css";

/** The lookup's own heading, so the field is reachable by name and not only by its label. */
export const LOOKUP_LABEL = "Open a session";

/** Where the id the lookup wants comes from, for a session the capped listing did not hold. */
export const LOOKUP_NOTE =
  "a session id is printed by every run command's --json, and by the rows above.";

export function SessionLookup() {
  return (
    <section className={styles.lookup} aria-label={LOOKUP_LABEL}>
      <p className={styles.note} data-testid="lookup-note">
        {LOOKUP_NOTE}
      </p>
      <IdLookup kind="session id" hrefFor={sessionHref} />
    </section>
  );
}
