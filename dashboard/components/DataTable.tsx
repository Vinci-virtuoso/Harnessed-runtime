"use client";

/**
 * A list response, shown as a table in the gateway's own order (`D-1`, FR-49; design.md inventory).
 *
 * The table offers no way to reorder itself. That is the whole point: the store's order is
 * "newest first" or "decided order", and a column sort would silently replace a fact the operator
 * came here to read with a preference the browser invented. There is no sort handler to disable,
 * no `aria-sort` to set and no header button to click - the order arrives with the rows and
 * leaves with them.
 *
 * `rows` is whatever identifies a row to the caller, in the response's array order; `columns.cell`
 * renders each one, normally as a `Value` of the same panel's response.
 *
 * `rowLink` is the row's one link, and it is the first cell's anchor rather than a wrapper around
 * a cell: an anchor wrapped around a `Value` would swallow that figure's `SourceChip`, and a
 * button inside a link is a nested control that no keyboard can use sensibly. So the link is the
 * id itself - the anchor carries the figure's own route and path, its text is `leaf.text`
 * verbatim, and the href is built by the caller from that same leaf (MUST rule 25). The rest of
 * the row stays selectable text: the whole row is never clickable.
 */
import type { ReactNode } from "react";

import type { Leaf } from "../lib/wire";

import styles from "./DataTable.module.css";

/** One column: its header, and what a row shows underneath it. */
export type Column<Row> = {
  /** The header text, rendered in a `th scope="col"`. */
  header: string;
  /** This row's cell. A figure here is a `Value`, never a computed string. */
  cell: (row: Row) => ReactNode;
};

/** The row's link, rendered as the first cell's anchor over the row's own id. */
export type RowLink<Row> = {
  header: string;
  /** The row's id, as a leaf of the response this panel fetched. Its text is the link's text. */
  leaf: (row: Row) => Leaf;
  /** Where the row goes. Built by the caller from the same leaf, never from a reformatted id. */
  href: (row: Row) => string;
};

type Props<Row> = {
  /** Names the table for assistive tech, rendered as its `caption`. */
  caption: string;
  columns: readonly Column<Row>[];
  /** The response's rows, in the gateway's order. Never sorted, never filtered here. */
  rows: readonly Row[];
  /** The row's link, where rows link at all. */
  rowLink?: RowLink<Row>;
};

export function DataTable<Row>({ caption, columns, rows, rowLink }: Props<Row>) {
  return (
    <div className={styles.scroll}>
      <table className={styles.table}>
        <caption className={styles.caption}>{caption}</caption>
        <thead>
          <tr>
            {rowLink === undefined ? null : (
              <th scope="col" className={styles.head}>
                {rowLink.header}
              </th>
            )}
            {columns.map((column) => (
              <th key={column.header} scope="col" className={styles.head}>
                {column.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {[...rows.entries()].map(([index, row]) => (
            <tr key={index} className={styles.row}>
              {rowLink === undefined ? null : <td className={styles.cell}>{link(rowLink, row)}</td>}
              {columns.map((column) => (
                <td key={column.header} className={styles.cell}>
                  {column.cell(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** The first cell's anchor: the row's id, as the response spelled it, and nothing else. */
function link<Row>(rowLink: RowLink<Row>, row: Row): ReactNode {
  const leaf = rowLink.leaf(row);
  return (
    <a
      className={styles.link}
      href={rowLink.href(row)}
      data-figure=""
      data-route={leaf.route}
      data-path={leaf.path}
    >
      {leaf.text}
    </a>
  );
}
