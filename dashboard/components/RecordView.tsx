"use client";

/**
 * A structured value as a field list (FR-127, `D-10`): tool arguments, a request, a JSON result.
 * A two-column `dl`, nested objects one level open, deeper ones behind a "show fields" disclosure,
 * arrays as bulleted rows (design.md's `record` presenter row).
 *
 * The served text is JSON, so reading it structurally is not "choosing a presenter from the
 * content" (MUST rule 32 governs which presenter `OutputView` picks, not how the `record`
 * presenter reads what it was already told is a record). Every scalar keeps decision 31's exact
 * characters - `1.0` stays `1.0` - through the same source-text reviver `lib/wire.ts` uses, kept
 * here rather than imported because that module exports no unboxed tree, only a flat leaf map.
 */
import { useState } from "react";

import type { Leaf } from "../lib/wire";

import styles from "./RecordView.module.css";

type Props = {
  output: Leaf;
  /**
   * The jq path `ProvenanceDrawer` opened on (FR-129, `D-4`): the one field this record was read
   * out of. Marked `aria-current="true"` and highlighted, and its own disclosure - and every
   * disclosure above it - opens by default so the cited field is on screen without a click.
   */
  citedPath?: string;
};

/** A JSON primitive, kept as both its parsed value (for a string's own characters) and its exact
 * source text (for a number, boolean or null - decision 31). */
class Primitive {
  constructor(
    readonly source: string,
    readonly parsed: unknown,
  ) {}
}

type SourceContext = { source?: string };
type SourceReviver = (this: unknown, key: string, value: unknown, context?: SourceContext) => unknown;
type SourceAwareJson = { parse: (text: string, reviver: SourceReviver) => unknown };

/** A revived node: a boxed primitive, or a plain array/object whose own children are nodes. */
type Node = Primitive | Node[] | { [key: string]: Node };

function parseExact(text: string): Node | undefined {
  const json = JSON as unknown as SourceAwareJson;
  try {
    return json.parse(text, (_key, value, context) => {
      const source = context?.source;
      return source === undefined ? value : new Primitive(source, value);
    }) as Node;
  } catch {
    return undefined;
  }
}

/** The characters a scalar renders as: a string's own text, or a number/boolean/null's source. */
function scalarText(box: Primitive): string {
  return typeof box.parsed === "string" ? box.parsed : box.source;
}

/** How deep to open a nested container before it hides behind its own disclosure. */
const OPEN_DEPTH = 1;

/** The jq path a node's own `path` segments spell, in `lib/wire.ts`'s own format (`.a[2].b`). */
function jqPath(path: readonly string[]): string {
  let built = "";
  for (const segment of path) built = segment.startsWith("[") ? `${built}${segment}` : `${built}.${segment}`;
  return built;
}

export function RecordView({ output, citedPath }: Props) {
  const tree = parseExact(output.text);
  if (tree === undefined) {
    return (
      <p className={styles.value} data-testid="record-unreadable">
        {output.text}
      </p>
    );
  }
  return (
    <div data-testid="record-view">
      <Container node={tree} path={[]} cited={citedPath} />
    </div>
  );
}

/** A node at the field list's own top level, or inside an already-open disclosure. */
function Container({
  node,
  path,
  cited,
}: {
  node: Node;
  path: readonly string[];
  cited: string | undefined;
}) {
  if (Array.isArray(node)) {
    return (
      <ul className={styles.list} data-testid="record-array">
        {node.map((item, index) => (
          <li key={`${path.join(".")}[${index}]`}>
            <FieldValue node={item} path={[...path, `[${index}]`]} cited={cited} />
          </li>
        ))}
      </ul>
    );
  }
  if (node instanceof Primitive) {
    return <FieldValue node={node} path={path} cited={cited} />;
  }
  return (
    <dl className={styles.record}>
      {Object.entries(node).map(([key, value]) => (
        <FieldRow key={key} fieldKey={key} value={value} path={[...path, key]} cited={cited} />
      ))}
    </dl>
  );
}

function FieldRow({
  fieldKey,
  value,
  path,
  cited,
}: {
  fieldKey: string;
  value: Node;
  path: readonly string[];
  cited: string | undefined;
}) {
  return (
    <>
      <dt className={styles.term}>{fieldKey}</dt>
      <dd className={styles.value}>
        <FieldValue node={value} path={path} cited={cited} />
      </dd>
    </>
  );
}

/** One field's value: a scalar rendered verbatim, or a container - open, or behind a disclosure. */
function FieldValue({
  node,
  path,
  cited,
}: {
  node: Node;
  path: readonly string[];
  cited: string | undefined;
}) {
  const isCited = cited !== undefined && cited === jqPath(path);
  if (node instanceof Primitive) {
    const text = scalarText(node);
    return (
      <span
        className={
          typeof node.parsed === "string"
            ? styles.string
            : isCited
              ? `${styles.value} ${styles.cited}`
              : styles.value
        }
        data-testid={isCited ? "cited" : undefined}
        aria-current={isCited ? "true" : undefined}
      >
        {text}
      </span>
    );
  }
  // `path.length` is how many fields deep this container sits, counted with no arithmetic
  // (the no-derivation lint bans `+`/`-` in this tree, decision 32): a fresh array each step.
  if (path.length > OPEN_DEPTH) {
    return <Disclosure node={node} path={path} cited={cited} />;
  }
  return <Container node={node} path={path} cited={cited} />;
}

/** Does the cited path sit inside (or at) this subtree, so its own disclosure must open by default? */
function holdsCited(path: readonly string[], cited: string | undefined): boolean {
  if (cited === undefined) return false;
  return cited.startsWith(jqPath(path));
}

function Disclosure({
  node,
  path,
  cited,
}: {
  node: Node;
  path: readonly string[];
  cited: string | undefined;
}) {
  const [open, setOpen] = useState(() => holdsCited(path, cited));
  return (
    <div className={styles.disclosure}>
      <button
        type="button"
        className={styles.toggle}
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        {open ? "Hide fields" : "Show fields"}
      </button>
      {open ? <Container node={node} path={path} cited={cited} /> : null}
    </div>
  );
}
