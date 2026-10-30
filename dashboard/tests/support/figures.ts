/**
 * The per-page walk FR-51 requires: **0 figures without both a route and a path**.
 *
 * Every screen ticket repeats this walk against its own page, so it lives here rather than in one
 * test file. A "figure" is anything that claims to be one - `Value`'s `data-figure` marker, or any
 * element already carrying one half of the provenance pair - and the walk reports every claim that
 * is missing either half. That is what makes it able to fail (MUST rule 12): a hand-written
 * `<span data-route="/v1/x">0.8</span>` is found, and so is a `Value` that lost its path.
 *
 * It is the pairing half of the oracle. `tests/support/source-at.ts` is the other half: it checks
 * that the characters at the cited path are the characters rendered.
 */

/** One figure that cannot say where it came from. */
export type SourcelessFigure = {
  /** What the operator sees, so a failure names the figure rather than a selector. */
  rendered: string;
  /** The half that was present, or the empty string where it was not. */
  route: string;
  path: string;
};

/** Anything that claims to be a figure: the marker, or either half of the provenance pair. */
const FIGURE = "[data-figure],[data-route],[data-path]";

/** Every figure in `container` whose route or path is missing. Empty means every figure has both. */
export function figuresWithoutSource(container: ParentNode): SourcelessFigure[] {
  const found: SourcelessFigure[] = [];
  for (const node of container.querySelectorAll(FIGURE)) {
    const route = node.getAttribute("data-route");
    const path = node.getAttribute("data-path");
    if (route && path) continue;
    found.push({ rendered: node.textContent ?? "", route: route ?? "", path: path ?? "" });
  }
  return found;
}
