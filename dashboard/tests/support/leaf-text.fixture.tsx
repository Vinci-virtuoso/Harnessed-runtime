/**
 * Seeded violations of decision 42 for `tests/unit/no-derivation.test.ts` (FR-86): a `Leaf`'s text
 * lifted out and handed to a component as a bare `string`. It sits outside the rule's globs, so
 * `npm run lint` stays green; the test lints this text as if it lived under `components/`.
 *
 * The last element is the sanctioned shape and must not be reported: the `Leaf` itself travels.
 */
import type { ReactNode } from "react";

import type { Leaf } from "../../lib/wire";

function Box({ label, children }: { label?: string | Leaf; children?: ReactNode }) {
  return <g aria-label={typeof label === "string" ? label : undefined}>{children}</g>;
}

export function Lifted({ leaf }: { leaf: Leaf }) {
  return (
    <>
      <Box label={leaf.text} />
      <Box label={`${leaf.text} turns`} />
      <Box label={leaf} />
    </>
  );
}
