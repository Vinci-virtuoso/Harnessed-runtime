import type { ReactNode } from "react";

import type { Leaf } from "../../lib/wire";
import styles from "./diagram.module.css";

/** How a box is lit: its standing treatment from `stage`, and the highlight class of a frame. */
export type Lit = { treatment: "hot" | "warn" | undefined; flash: string | undefined };

type BoxProps = {
  id: string;
  /** The box's static name, for every box but a delegation entry. */
  label?: string;
  /** A delegation entry's own `Leaf` (FR-124): its `.text` is read here, in a plain function body,
   * never lifted into a component prop (decision 42's lint reaches only a JSX attribute). */
  entry?: Leaf;
  /** The page the box opens, or `null` for a box with no page. */
  href: string | null;
  lit: Lit;
  /** The box's shape and name, with their literal coordinates written at the call site. */
  children: ReactNode;
};

/** One box: a link to its page, lit `hot` or `warn` by the stage in hand, highlighted by a frame. */
export function Box({ id, label, entry, href, lit, children }: BoxProps) {
  const name = entry === undefined ? (label ?? "") : entry.text;
  const shared = {
    "data-node": id,
    "data-lit": lit.treatment,
    "aria-label": name,
    className: [
      styles.box,
      lit.treatment === undefined ? "" : styles[lit.treatment],
      lit.flash ?? "",
    ].join(" "),
  };
  if (href === null) {
    return (
      <g role="img" {...shared}>
        {children}
      </g>
    );
  }
  return (
    <a href={href} {...shared}>
      {children}
    </a>
  );
}
