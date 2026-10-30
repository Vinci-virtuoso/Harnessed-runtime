/**
 * Which pages ship solid rather than glass (design.md "Glass", G-4 and G-5; decision 84).
 *
 * Empty at the start of v6. A page is named here only when the operator's scroll check on the
 * longest page (Traces, 100 rows, integrated GPU) drops more than 5% of frames past 16.7 ms; the
 * fix is this one line and no token change. A listed page renders `data-surface="solid"` on its
 * root `<div>`, which the stylesheet's `:root:has([data-surface="solid"])` block answers by
 * restating the solid v5 values for the whole surface layer.
 */
export const SOLID_PAGES: readonly string[] = [];

/** The page a pathname belongs to: its first segment, the overview being `overview`. */
export function pageOf(pathname: string | null): string {
  const first = (pathname ?? "").split("/").find((segment) => segment !== "");
  return first ?? "overview";
}

/** The `data-surface` value a page's root takes: `"solid"` for a listed page, else absent. */
export function surfaceOf(
  pathname: string | null,
  pages: readonly string[] = SOLID_PAGES,
): "solid" | undefined {
  return pages.includes(pageOf(pathname)) ? "solid" : undefined;
}
