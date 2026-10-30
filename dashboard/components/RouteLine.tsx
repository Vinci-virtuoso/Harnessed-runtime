"use client";

/**
 * One decision, read in words: who Jev sent the message to, how sure it was, and, on a chain, that
 * this is one more hop of the same exchange (FR-122, UJ-16, UJ-18, UJ-20; design.md "The console in
 * v5", decision 61).
 *
 * The sentence is built from `HUMAN_WORDS` (`lib/human-words.ts`'s `route` and
 * `route_memory_clause` fields) and the decision's own served fields - never a computed
 * probability, never a derived percentage (`no-derivation.test.ts`, `D-1`). The confidence rides a
 * `SourceChip` labelled "Jev <percent>" rather than inline text, so the one figure that opens a
 * drawer is unambiguous.
 *
 * `first` distinguishes the exchange's own opening line ("Jev sent this to ...") from a chain's
 * later hops ("then to ...", "then done" once `route` is the chain's own terminal option).
 */
import { humanWord } from "../lib/human-words";
import type { Leaf, Wire } from "../lib/wire";

import { SourceChip } from "./SourceChip";
import { Value } from "./Value";
import styles from "./RouteLine.module.css";

/** `router/route.py`'s chain terminal: the last output becomes the reply, no further hop. */
const ROUTE_DONE = "done";

type Props =
  | {
      /** One row of `/v1/sessions/{id}/decisions`, scoped to this decision (decision 42). */
      decision: Wire;
      /** Whether this is the exchange's opening decision, or one more hop of a chain. */
      first: boolean;
      routeReason?: undefined;
    }
  | {
      decision?: undefined;
      first?: undefined;
      /** A message's served `route_reason`, where no route was chosen (routing off, ticket 317). */
      routeReason: Leaf;
    };

function routeHuman(route: Leaf): { text: string; exact: string } | undefined {
  const word = humanWord("route", route.text);
  return word === undefined ? undefined : { text: word, exact: route.text };
}

function memoryHuman(memoryKind: Leaf): { text: string; exact: string } | undefined {
  const word = humanWord("route_memory_clause", memoryKind.text);
  return word === undefined ? undefined : { text: word, exact: memoryKind.text };
}

/** "Jev <percent>" - the confidence chip's own label, never a derived figure (decision 42). */
function jevLabel(confidencePercent: Leaf): string {
  return `Jev ${confidencePercent.text}`;
}

export function RouteLine({ decision, first, routeReason }: Props) {
  if (routeReason !== undefined) {
    return (
      <p className={styles.line} data-testid="route-line">
        <span className={styles.sentence} data-testid="route-sentence">
          <Value leaf={routeReason} />
        </span>
      </p>
    );
  }
  const route = decision.at(".route");
  const memoryKind = decision.at(".memory_kind");
  const confidencePercent = decision.at(".confidence_percent");
  const done = route.text === ROUTE_DONE;
  const memoryClause = done || memoryKind.kind === "null" ? undefined : memoryHuman(memoryKind);

  return (
    <p className={styles.line} data-testid="route-line">
      <span className={styles.sentence} data-testid="route-sentence">
        {first ? "Jev sent this to " : done ? "then " : "then to "}
        {done ? "done" : <Value leaf={route} human={routeHuman(route)} />}
        {memoryClause === undefined ? null : (
          <>
            {" (memory: "}
            <Value leaf={memoryKind} human={memoryClause} />
            {")"}
          </>
        )}
      </span>
      {done ? null : <SourceChip label={jevLabel(confidencePercent)} leaf={confidencePercent} />}
    </p>
  );
}
