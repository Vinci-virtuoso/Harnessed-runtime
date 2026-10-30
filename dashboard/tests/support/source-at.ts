/**
 * The independent oracle for FR-45: what characters does the raw response body hold at a given
 * jq path, and do the rendered figures match them?
 *
 * It is deliberately a second implementation, written from the raw bytes and not from
 * `lib/wire.ts`, so a wire-layer bug shows up as a mismatch rather than agreeing with itself
 * (MUST rule 12: the comparison must be able to fail). It reads the DOM for `data-route` and
 * `data-path`, so it sees exactly what the operator sees.
 */

/** One rendered figure, as the DOM carries it. */
export type RenderedFigure = {
  route: string;
  path: string;
  rendered: string;
};

/** One disagreement between a rendered figure and the bytes at its source path. */
export type Divergence = {
  route: string;
  path: string;
  rendered: string;
  expected: string | undefined;
};

type SourceContext = { source?: string };
type SourceReviver = (this: unknown, key: string, value: unknown, context?: SourceContext) => unknown;
type SourceAwareJson = { parse: (text: string, reviver: SourceReviver) => unknown };

/** A primitive placeholder the reviver leaves behind so the walk can find the leaves. */
class Primitive {
  constructor(
    readonly source: string,
    readonly parsed: unknown,
  ) {}
}

/**
 * Every jq path in `text` that holds a primitive, mapped to the characters the body spells there.
 * Numbers, booleans and null give the source slice; a string gives its parsed value, which is
 * what a figure renders.
 */
export function sourceCharactersByPath(text: string): Map<string, string> {
  return walked(text).characters;
}

/**
 * Every jq path in `text` that holds a real `null`, as opposed to the string `"null"`. `D-2` says
 * a null is rendered as the `unknown` treatment and never as its own characters, so the two have
 * to be told apart for `divergences` to know which rendering is the faithful one.
 */
export function nullPaths(text: string): Set<string> {
  return walked(text).nulls;
}

/** What the word `unknown` is: the one rendering `D-2` allows for a `null` the gateway sent. */
export const UNKNOWN_RENDERING = "unknown";

function walked(text: string): { characters: Map<string, string>; nulls: Set<string> } {
  const json = JSON as unknown as SourceAwareJson;
  const tree = json.parse(text, function reviver(_key, value, context) {
    const source = context?.source;
    return source === undefined ? value : new Primitive(source, value);
  });
  const characters = new Map<string, string>();
  const nulls = new Set<string>();
  collect(tree, "", characters, nulls);
  return { characters, nulls };
}

function collect(node: unknown, path: string, into: Map<string, string>, nulls: Set<string>): void {
  if (node instanceof Primitive) {
    into.set(path, typeof node.parsed === "string" ? node.parsed : node.source);
    if (node.parsed === null) nulls.add(path);
    return;
  }
  if (Array.isArray(node)) {
    for (const [index, item] of node.entries()) collect(item, `${path}[${index}]`, into, nulls);
    return;
  }
  if (typeof node === "object" && node !== null) {
    for (const [key, item] of Object.entries(node)) collect(item, `${path}.${key}`, into, nulls);
  }
}

/** Every figure a rendered tree exposes, in DOM order, read from the provenance hooks. */
export function renderedFigures(container: ParentNode): RenderedFigure[] {
  return [...container.querySelectorAll("[data-path]")].map((node) => ({
    route: node.getAttribute("data-route") ?? "",
    path: node.getAttribute("data-path") ?? "",
    rendered: node.textContent ?? "",
  }));
}

/**
 * Compare every rendered figure with the characters at its source path in `bodies`, keyed by
 * route. An empty result means the render is faithful to the wire.
 *
 * The one substitution is `D-2`'s, added by ticket 101: where the body spells a `null`, the
 * faithful rendering is the word `unknown`, because a null is an absence and not a figure. It is
 * one way and total - a null may render as `unknown` and as nothing else, and `0`, `-` and a
 * blank are still reported here - so the walk stays able to fail on every other leaf.
 */
export function divergences(figures: readonly RenderedFigure[], bodies: ReadonlyMap<string, string>): Divergence[] {
  const byRoute = new Map<string, Map<string, string>>();
  const nullsByRoute = new Map<string, Set<string>>();
  for (const [route, text] of bodies) {
    byRoute.set(route, sourceCharactersByPath(text));
    nullsByRoute.set(route, nullPaths(text));
  }

  const found: Divergence[] = [];
  for (const figure of figures) {
    const characters = byRoute.get(figure.route)?.get(figure.path);
    const expected = nullsByRoute.get(figure.route)?.has(figure.path)
      ? UNKNOWN_RENDERING
      : characters;
    if (expected !== figure.rendered) {
      found.push({ route: figure.route, path: figure.path, rendered: figure.rendered, expected });
    }
  }
  return found;
}
