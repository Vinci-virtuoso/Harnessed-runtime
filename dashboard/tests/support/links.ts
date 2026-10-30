/**
 * The independent oracle for FR-60: **0** links on any page carry an id the page built.
 *
 * `tests/support/figures.ts` asks whether a figure can say where it came from and
 * `tests/support/source-at.ts` asks whether it renders the characters the wire sent. This is the
 * third question of the same shape, asked of links rather than figures: every variable part of an
 * internal href - a path segment that is not a route word, a query value that is an id - must
 * equal, character for character, the text of a string leaf in a response the page rendered.
 *
 * Equality is the whole check, and it is why splitting, trimming and reformatting are all caught
 * by one comparison rather than by three pattern matches: a case run key split on `:` is not a
 * leaf, a trimmed id is not a leaf, a re-cased id is not a leaf. There is no rule here that says
 * "no `.split()`" - a page could split an id and put it back together and still pass, and that is
 * correct, because what FR-60 forbids is a *built id* reaching a URL and not a call being made
 * (MUST rule 25).
 *
 * It reads the DOM and the raw response bytes, and imports nothing from `lib/`, so a defect in
 * the wire layer shows up as a mismatch here rather than agreeing with itself (MUST rule 12).
 * `tests/unit/links.test.tsx` seeds a link that splits a case run key and asserts this reports it.
 *
 * The one leniency, recorded rather than hidden: a segment passes if it is a route word **or** a
 * leaf, so an id whose characters happen to spell a route word (`facts`, `compare`) would pass.
 * Nothing the gateway mints can - every id it sends is prefixed and long - and the alternative,
 * ordering the two tests the other way round, is exactly as lenient.
 */

/** One internal link, as the DOM carries it, and the page it was found on. */
export type PageLink = {
  /** The page URL an operator would be at, so a failure names a screen and not a container. */
  at: string;
  href: string;
  /** The link's visible text, which for a row link is the id itself. */
  text: string;
};

/** One part of one href that no response ever spelled. */
export type BuiltId = {
  at: string;
  href: string;
  /** The characters that are in the URL and in no response. */
  part: string;
  /** Where in the href they sit, so a failure reads as a sentence. */
  where: string;
};

/** How an href's parts are told apart. Every name here is a page's, never a gateway's. */
export type LinkVocabulary = {
  /** The literal directory names of the route tree. A segment that is one of these is not an id. */
  staticSegments: ReadonlySet<string>;
  /** Query parameters that carry an id, and so must be a leaf verbatim. */
  idParams: ReadonlySet<string>;
  /** Query parameters that carry an operator's own choice - a limit, a search - and no id. */
  controlParams: ReadonlySet<string>;
};

/** A base no page is served from, so a relative href can be parsed without inventing an origin. */
const BASE = "http://dashboard.invalid";

/**
 * Every internal link a rendered page holds, in DOM order.
 *
 * Internal means the href starts with `/`: the in-page skip link (`#main`) goes nowhere else, and
 * a `SourceChip`'s absolute gateway URL is the operator leaving the dashboard for the gateway's
 * own answer, which is a route this page already fetched and not a link between two pages.
 */
export function internalLinks(at: string, container: ParentNode): PageLink[] {
  return [...container.querySelectorAll("a[href]")]
    .map((node) => ({ at, href: node.getAttribute("href") ?? "", text: node.textContent ?? "" }))
    .filter((link) => link.href.startsWith("/"));
}

/**
 * Every string a response spells, read from the raw bytes by a plain parse.
 *
 * Strings only, and deliberately: an id is a string on this wire, and a number's characters are
 * `lib/wire.ts`'s problem and `source-at.ts`'s check. A key is not a value, so `{"run_key": ...}`
 * contributes the id and not the word `run_key`.
 */
export function stringLeaves(text: string): Set<string> {
  const found = new Set<string>();
  collect(JSON.parse(text) as unknown, found);
  return found;
}

function collect(node: unknown, into: Set<string>): void {
  if (typeof node === "string") {
    into.add(node);
    return;
  }
  if (Array.isArray(node)) {
    for (const item of node) collect(item, into);
    return;
  }
  if (typeof node === "object" && node !== null) {
    for (const item of Object.values(node)) collect(item, into);
  }
}

/** Every string leaf of every body a page rendered, which is the set its links may draw on. */
export function leavesOf(bodies: Iterable<string>): Set<string> {
  const found = new Set<string>();
  for (const text of bodies) for (const leaf of stringLeaves(text)) found.add(leaf);
  return found;
}

/**
 * Every part of every link that no response spelled. An empty result means 0 built ids.
 *
 * A part is compared after URL-decoding, because "as typed, URL-encoded only" is what FR-60 allows
 * a URL to do to an id: `%20` and a space are the same id, and neither is a reformatting. Where
 * the characters are not a valid encoding at all - an id holding a bare `%` - the raw characters
 * are compared instead, so an id is never rejected for the decoder's sake.
 */
export function builtIds(
  links: readonly PageLink[],
  leaves: ReadonlySet<string>,
  vocabulary: LinkVocabulary,
): BuiltId[] {
  const found: BuiltId[] = [];

  for (const link of links) {
    const url = new URL(link.href, BASE);

    for (const segment of url.pathname.split("/")) {
      if (segment === "") continue;
      const part = decoded(segment);
      if (vocabulary.staticSegments.has(part)) continue;
      if (leaves.has(part)) continue;
      found.push({ at: link.at, href: link.href, part, where: "path segment" });
    }

    for (const [name, value] of url.searchParams) {
      if (vocabulary.controlParams.has(name)) continue;
      if (!vocabulary.idParams.has(name)) {
        found.push({ at: link.at, href: link.href, part: name, where: "unnamed query parameter" });
        continue;
      }
      if (leaves.has(value)) continue;
      found.push({ at: link.at, href: link.href, part: value, where: `query ?${name}=` });
    }
  }

  return found;
}

/** Every query parameter name any of these links carries, so the vocabulary can be proved total. */
export function queryParams(links: readonly PageLink[]): Set<string> {
  const names = new Set<string>();
  for (const link of links) {
    for (const [name] of new URL(link.href, BASE).searchParams) names.add(name);
  }
  return names;
}

/**
 * What an operator typed, recovered from the URL a lookup sent them to.
 *
 * The prefix is the target page's own path, so what is left is the id and nothing else. It is
 * decoded, because URL-encoding is the one thing FR-60 lets a lookup do; every other difference
 * from what was typed is a built id.
 */
export function idCarriedBy(href: string, prefix: string): string {
  if (!href.startsWith(prefix)) throw new Error(`${href} does not open ${prefix}`);
  return decoded(href.slice(prefix.length));
}

/** The characters a URL part spells, or the part itself where it is not an encoding at all. */
function decoded(part: string): string {
  try {
    return decodeURIComponent(part);
  } catch {
    return part;
  }
}
