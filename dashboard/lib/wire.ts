/**
 * Wire-faithful parsing (decision 31, MUST rule 21). The dashboard renders the characters the
 * gateway sent, so a score sent as `1.0` must not come back from the parser as `1`.
 *
 * `JSON.parse`'s reviver takes a third argument whose `source` holds the body's own characters for
 * every primitive. `parseWire` keeps that slice on each leaf, together with the leaf's jq-style
 * path and the route the panel fetched, and hands components `Leaf` values only: this module
 * exports no numeric type at all, so no component can be handed a re-serialized figure
 * (decision 32).
 *
 * There is no downgrade path. The module probes the parser once at load; if the probe cannot see
 * source text, `parseWire` throws `WireUnsupported` and every panel renders `ErrorState`. Falling
 * back to a plain parse would render plausible figures that are not what the wire said, which is
 * the one failure this milestone exists to prevent.
 */

/** What the body spelled at a leaf. Strings carry the parsed value; the rest carry the slice. */
export type LeafKind = "number" | "string" | "boolean" | "null";

/** One figure, exactly as its response spelled it, with the provenance a panel renders beside it. */
export type Leaf = {
  kind: LeafKind;
  /** The characters to render. Never rounded, padded, reformatted or recomputed. */
  text: string;
  /** jq-style, as `ux/design.md` spells it: `.usage.input_tokens`, `.runs[0].score`. */
  path: string;
  /** The gateway route this leaf came from, so a figure can name its own source. */
  route: string;
};

/**
 * One parsed response. `text` and `byteLength` are the raw body and its UTF-8 length, kept for
 * `ProvenanceDrawer` (FR-51); the length is a string because it is displayed, never calculated.
 */
export type Wire = {
  route: string;
  text: string;
  byteLength: string;
  /** The leaf at a jq path. Throws when the response has no such path. */
  at: (path: string) => Leaf;
};

/** Joined rather than concatenated: `+` is banned in this tree, strings included (decision 32). */
const UNSUPPORTED_MESSAGE = [
  "This browser's JSON.parse does not expose a value's source text, so the dashboard cannot",
  "prove a figure is the one the gateway sent (decision 31).",
  "Nothing is rendered from this response.",
].join(" ");

/** The parser cannot show the body's own characters, so nothing may be rendered from it. */
export class WireUnsupported extends Error {
  constructor(message: string) {
    super(message);
    this.name = "WireUnsupported";
  }
}

type SourceContext = { source?: string };
type SourceReviver = (this: unknown, key: string, value: unknown, context?: SourceContext) => unknown;
type SourceAwareJson = { parse: (text: string, reviver: SourceReviver) => unknown };

/** A revived primitive, kept boxed so the path walk can tell a leaf from a container. */
class Primitive {
  constructor(
    readonly source: string,
    readonly parsed: unknown,
  ) {}
}

const sourceAwareJson = (): SourceAwareJson => JSON as unknown as SourceAwareJson;

/** What this parser reports as the source text of `1.0`. `"1.0"` is the only acceptable answer. */
function probeSourceText(): string | undefined {
  let seen: string | undefined;
  sourceAwareJson().parse("1.0", (_key, value, context) => {
    seen = context?.source;
    return value;
  });
  return seen;
}

const SOURCE_TEXT_PROBE = probeSourceText();

function kindOf(parsed: unknown): LeafKind {
  if (typeof parsed === "string") return "string";
  if (typeof parsed === "boolean") return "boolean";
  if (parsed === null) return "null";
  return "number";
}

function textOf(box: Primitive): string {
  return typeof box.parsed === "string" ? box.parsed : box.source;
}

function walk(node: unknown, path: string, route: string, into: Map<string, Leaf>): void {
  if (node instanceof Primitive) {
    into.set(path, { kind: kindOf(node.parsed), text: textOf(node), path, route });
    return;
  }
  if (Array.isArray(node)) {
    for (const [index, item] of node.entries()) walk(item, `${path}[${index}]`, route, into);
    return;
  }
  // The reviver boxes every primitive, `null` included, so what is left here is a plain object.
  for (const [key, item] of Object.entries(node as Record<string, unknown>)) {
    walk(item, `${path}.${key}`, route, into);
  }
}

/**
 * Parse a gateway response body into leaves that carry its own characters.
 *
 * @param text the raw response body, exactly as received
 * @param route the gateway route it came from, carried onto every leaf
 */
export function parseWire(text: string, route: string): Wire {
  if (SOURCE_TEXT_PROBE !== "1.0") {
    throw new WireUnsupported(UNSUPPORTED_MESSAGE);
  }

  const tree = sourceAwareJson().parse(text, (_key, value, context) => {
    const source = context?.source;
    return source === undefined ? value : new Primitive(source, value);
  });

  const leaves = new Map<string, Leaf>();
  walk(tree, "", route, leaves);

  return {
    route,
    text,
    byteLength: String(new TextEncoder().encode(text).length),
    at: (path: string): Leaf => {
      const leaf = leaves.get(path);
      if (leaf === undefined) {
        throw new Error(`The response from ${route} has no leaf at ${path}`);
      }
      return leaf;
    },
  };
}
