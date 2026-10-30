"use client";

/**
 * Paste an id, open its page (FR-60, FR-57; design.md inventory).
 *
 * Some surfaces have no listing to click through - an improvement cycle is reached by its id and
 * by nothing else (`[GAP G-8]`) - so this is the one place an operator types rather than follows
 * a link. What they type is sent exactly as typed: no trim, no split on `:`, no reformat, because
 * the moment a lookup edits an id it is building one, and a built id is the defect MUST rule 25
 * exists to prevent.
 *
 * It never asks whether the id exists. There is no "checking" state and no request: the target
 * page fetches the route, and a 404 arrives there as `AbsentState` quoting the gateway's own
 * message. A lookup that pre-checked would be a second, quieter source of truth about what the
 * ledger holds.
 *
 * `Open` is disabled while the field is blank, so the one submission that could go nowhere cannot
 * be made, and the label names the kind of id this field expects rather than leaving the operator
 * to guess which of three ids belongs here.
 */
import { useId, useState } from "react";

import { useRouter } from "next/navigation";

import styles from "./IdLookup.module.css";

type Props = {
  /** The id this field expects, named in its visible label. */
  kind: "session id" | "improvement run id" | "eval run id";
  /** The page this id opens. The id arrives here exactly as it was typed. */
  hrefFor: (id: string) => string;
};

export function IdLookup({ kind, hrefFor }: Props) {
  const router = useRouter();
  const field = useId();
  const [id, setId] = useState("");
  const blank = id === "";

  return (
    <form
      className={styles.lookup}
      onSubmit={(event) => {
        event.preventDefault();
        if (blank) return;
        router.push(hrefFor(id));
      }}
    >
      <label className={styles.label} htmlFor={field}>
        {kind}
      </label>
      <input
        id={field}
        className={styles.input}
        type="text"
        autoComplete="off"
        spellCheck={false}
        value={id}
        onChange={(event) => setId(event.target.value)}
      />
      <button type="submit" className={styles.open} disabled={blank}>
        Open
      </button>
    </form>
  );
}
