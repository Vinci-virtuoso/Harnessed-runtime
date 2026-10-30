/**
 * The URL a page under test is rendered at (decision 45).
 *
 * Every query page reads `useSearchParams()` from `next/navigation`; outside the App Router that
 * hook has no context, so a test file mocks the module and points the hook here. `pageAt` sets
 * the query and renders the page, so a test says "this page, at this URL" in one call, exactly as
 * it said "this page, with these `searchParams`" while the pages were server-rendered.
 */
import type { ReactNode } from "react";

/** The query the next render reads. Set by `pageAt`, read by the mocked hook. */
export const query: { current: Record<string, string> } = { current: {} };

/** What the mocked `useSearchParams` returns: the query as a browser would have decoded it. */
export function useSearchParams(): URLSearchParams {
  return new URLSearchParams(query.current);
}

/** The page rendered at `?a=b&...`: the values are the decoded characters, verbatim. */
export function pageAt(Page: () => ReactNode, params: Record<string, string> = {}): ReactNode {
  query.current = params;
  return <Page />;
}
