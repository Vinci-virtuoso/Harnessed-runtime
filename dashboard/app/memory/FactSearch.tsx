"use client";

/**
 * The dashboard's one search box (FR-58, UJ-10).
 *
 * What is typed is sent exactly as typed: no trim, no quoting, no rewriting into an FTS
 * expression, because `memory/semantic.py::search` passes the query through as typed and whether
 * an expression is servable is the gateway's answer to give. Submitting is a navigation and
 * nothing else - the query lands in `?q=`, the page reads it back, and that is the whole of how a
 * search survives a reload and travels in a shared link (`D-6`).
 *
 * `Search` is disabled while the box is blank, because the gateway serves `[]` without a `q` and a
 * submission with nothing in it would turn the prompt state into a count of an empty store, which
 * is the one thing `[GAP G-14]` says this screen must not do.
 */
import { useId, useState } from "react";

import { useRouter } from "next/navigation";

import { factsHref } from "./search";
import styles from "./memory.module.css";

/** The box's visible label, which is also its accessible name. */
export const SEARCH_LABEL = "search facts";

/** The word on the button, so a test presses what an operator presses. */
export const SEARCH_BUTTON = "Search";

type Props = {
  /** The query the URL is carrying, as its characters, or nothing where it carries none. */
  q?: string;
};

export function FactSearch({ q }: Props) {
  const router = useRouter();
  const field = useId();
  const [query, setQuery] = useState(q ?? "");
  const blank = query === "";

  return (
    <form
      className={styles.search}
      onSubmit={(event) => {
        event.preventDefault();
        if (blank) return;
        router.push(factsHref(query));
      }}
    >
      <label className={styles.searchLabel} htmlFor={field}>
        {SEARCH_LABEL}
      </label>
      <input
        id={field}
        className={styles.searchField}
        type="text"
        autoComplete="off"
        spellCheck={false}
        value={query}
        onChange={(event) => setQuery(event.target.value)}
      />
      <button type="submit" className={styles.searchButton} disabled={blank}>
        {SEARCH_BUTTON}
      </button>
    </form>
  );
}
