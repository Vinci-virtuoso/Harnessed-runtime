/**
 * The two ids `/evals/compare` is a comparison of, and the two URLs they live in (FR-56, FR-62).
 *
 * Both ids are carried by the page's own URL and by nothing else (`D-6`), so a comparison is
 * reload-safe and shareable without a byte being remembered. They are characters throughout: an
 * eval run id is spelled into a route and never taken apart, because the gateway is the only
 * thing that takes an id apart (MUST rule 25). Nothing here trims, re-cases or splits one, and an
 * id the gateway does not hold comes back as its own 404 rather than as a guess made here.
 *
 * `compareRoute` is the one gateway route this page reads. `sideHref` is where a lookup sends the
 * operator: the side just typed, plus whatever side the URL already held, so filling the second
 * field never drops the first.
 */
import { compareHref } from "./limit";

/** The two sides of a comparison, in the order the page reads them. */
export type Side = "before" | "after";

/** Both sides, named once so a page never spells one of them and forgets the other. */
export const SIDES: readonly Side[] = ["before", "after"];

/** The gateway route used by the eval listing. */
export const COMPARE_ROUTE = "/v1/eval/compare";

/**
 * The route for one comparison. Both ids arrive as the URL spelled them and leave the same way.
 *
 * @param before the before side's eval run id, verbatim
 * @param after the after side's eval run id, verbatim
 */
export function compareRoute(before: string, after: string): string {
  return `${COMPARE_ROUTE}?before=${before}&after=${after}`;
}

/**
 * Where one side's lookup opens. With the other side already in the URL this is the full
 * comparison; with no other side it is this side alone, and the page asks for the second.
 *
 * @param side which side was typed
 * @param id what was typed, exactly as typed
 * @param other the side the URL already held, or `undefined` where it held neither
 */
export function sideHref(side: Side, id: string, other: string | undefined): string {
  if (other === undefined) return `/evals?${side}=${id}`;
  return side === "before" ? compareHref(id, other) : compareHref(other, id);
}
