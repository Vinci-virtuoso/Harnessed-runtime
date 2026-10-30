/**
 * How many rows a response sent, asked of the parsed response itself.
 *
 * `lib/wire.ts` hands out one leaf at a time and no container, which is what stops a component
 * from ever holding a parsed object it could compute over (decision 32). A table still has to
 * know where the gateway's array ended, so it asks the wire for the next row's id until the wire
 * says there is none: a probe, in the response's own order, that reads nothing but leaves.
 *
 * There is no counting here and no arithmetic: the next index is the length of the list of
 * indices found so far, so a row is only ever appended after the response confirmed it exists.
 * Nothing is sorted, reversed or filtered - the indices come out in the order the body spells
 * them, which is the gateway's order and the only order this dashboard shows (`D-1`).
 */
import type { Leaf, Wire } from "./wire";

/** The leaf at a path, or nothing where the response did not send that path at all. */
export function leafIfSent(wire: Wire, path: string): Leaf | undefined {
  try {
    return wire.at(path);
  } catch {
    return undefined;
  }
}

/**
 * The indices of an array in a response, in its own order.
 *
 * @param wire the parsed response the rows belong to
 * @param probe a field every row of the array has, written for one index
 */
export function rowsOf(wire: Wire, probe: (index: number) => string): number[] {
  const rows: number[] = [];
  while (leafIfSent(wire, probe(rows.length)) !== undefined) {
    rows.push(rows.length);
  }
  return rows;
}
