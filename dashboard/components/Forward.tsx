"use client";

/**
 * The stub an old URL is left as when its page's content moved to another page (operator ruling,
 * 2026-09-30).
 *
 * It reads the query the old page was opened with, maps each old parameter to the new page's by
 * the table it is handed, and replaces the history entry with the new URL once, in an effect.
 * Until that runs, and wherever it cannot, it renders a plain "Moved to <Page>" link to the same
 * URL. Every value is copied exactly as the URL carried it and re-encoded once, never trimmed,
 * split or reformatted (MUST rule 25); a parameter the table does not name is dropped.
 */
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect } from "react";

type Props = {
  /** The new page's path, e.g. `/traces`. */
  to: string;
  /** The new page's name, as the nav spells it. */
  page: string;
  /** Old parameter name -> new parameter name. */
  params: Readonly<Record<string, string>>;
  /** A fragment to land on, without the `#`, when the old page is a section of the new one. */
  hash?: string;
};

/** The URL the old one forwards to, from the query it was opened with. */
export function forwardHref(
  to: string,
  params: Readonly<Record<string, string>>,
  query: URLSearchParams,
  hash?: string,
): string {
  const next = new URLSearchParams();
  for (const [from, into] of Object.entries(params)) {
    const value = query.get(from);
    if (value !== null) next.set(into, value);
  }
  const text = next.toString();
  const url = text === "" ? to : `${to}?${text}`;
  return hash === undefined ? url : `${url}#${hash}`;
}

export function Forward({ to, page, params, hash }: Props) {
  const router = useRouter();
  const query = useSearchParams();
  const href = forwardHref(to, params, query, hash);

  useEffect(() => {
    router.replace(href);
  }, [router, href]);

  return (
    <p data-testid="forward-link">
      <Link href={href}>Moved to {page}</Link>
    </p>
  );
}
