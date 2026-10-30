/**
 * Seeded derivations for `tests/unit/no-derivation.test.ts`. It sits outside the rule's globs, so
 * `npm run lint` stays green; the test lints this text as if it lived under `lib/`.
 */
type Row = { name: string; count: number };

export function Delta({ before, after }: { before: number; after: number }) {
  return <span>{before - after}</span>;
}

export function sortedRows(rows: Row[]): Row[] {
  return rows.sort();
}
