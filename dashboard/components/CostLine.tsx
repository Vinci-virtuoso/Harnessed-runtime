"use client";

/**
 * The cost of one exchange, or the session's whole spend (FR-122, `D-2`; design.md's `CostLine`
 * row). Inline (no `total`), it renders one bare figure per row in `handlers` - the reply
 * caption's own "0.0031" / "subscription, API-equivalent 0.38" / "subscription, not metered".
 * Given a `total`, it renders the side rail's list instead: each row with its own `SourceChip`,
 * then the session total, then "API-equivalent, not in the total" on its own row.
 *
 * `billing` decides the figure (FR-115): `null` or `openrouter` shows the bare `cost`; `oauth`
 * shows `apiEquivalent` where reported, else the fixed "not metered" words - never the generic
 * `unknown` word `Value` always shows, so a screen reader hears why in the same breath ("not
 * metered, the agent reported no cost", `D-2` as amended). An unpriced `null`/`openrouter` row
 * reads the served `.reason` (decision 64: the store's own explanation for *this* handler) and
 * falls back to `lib/null-reasons.ts` only where the gateway sent none.
 */
import { rowsOf } from "../lib/rows";
import { nullReasonFor } from "../lib/null-reasons";
import type { Leaf, Wire } from "../lib/wire";

import { SourceChip } from "./SourceChip";
import { Value } from "./Value";
import styles from "./CostLine.module.css";

const BILLING_OAUTH = "oauth";
const SUBSCRIPTION = "subscription";
const NOT_METERED = "not metered";
const NOT_METERED_ANNOUNCED = `${NOT_METERED}, the agent reported no cost`;
const TOTAL_LABEL = "Total";
const API_EQUIVALENT_LABEL = "API-equivalent, not in the total";

type Props = {
  /** `.handlers`, scoped so `[N]` addresses one row (decision 42: a `Wire` subtree travels). */
  handlers: Wire;
  /** The session's own spend total; its presence is what switches to the side rail's list form. */
  total?: Leaf;
  apiEquivalent?: Leaf;
};

/** A row's own name, read through a named function so `leaf.text` never reaches a component prop
 * directly (decision 42's own lint; `RouteLine.tsx::jevLabel` is the same precedent). */
function handlerLabel(handler: Leaf): string {
  return handler.text;
}

/** Why this row's cost is unknown: the store's own explanation first, the client copy only where
 * the gateway sent none (decision 64 amended, ticket 189's own note on `ReplyCaption`'s prior
 * stand-in). */
function costReason(cost: Leaf, reason: Leaf): string | undefined {
  return reason.kind === "null" ? nullReasonFor(cost.route, cost.path) : reason.text;
}

/** The fixed "not metered" chip: `Value`'s own `unknown` treatment, with this component's own
 * word and its own announced sentence - never `Value`'s hard-coded "unknown" (`D-2` amended). */
function NotMetered() {
  return (
    <span className={styles.unknown} aria-label={NOT_METERED_ANNOUNCED}>
      {NOT_METERED}
    </span>
  );
}

/** One row's own money figure: the bare cost, the subscription's API-equivalent, or "not metered".
 * **0** rows render `$0.00`, `0` or `-` for a cost nobody reported (`D-2`, FR-122): a null cost
 * always renders through `Value`'s `unknown` treatment or this file's own `NotMetered`, never a
 * zero-looking literal. */
function CostFigure({ handlers, row }: { handlers: Wire; row: number }) {
  const billing = handlers.at(`[${row}].billing`);
  const cost = handlers.at(`[${row}].cost`);
  const apiEquivalent = handlers.at(`[${row}].api_equivalent`);
  const reason = handlers.at(`[${row}].reason`);

  if (billing.kind !== "null" && billing.text === BILLING_OAUTH) {
    if (apiEquivalent.kind === "null") {
      return (
        <>
          {SUBSCRIPTION}
          {", "}
          <NotMetered />
        </>
      );
    }
    return (
      <>
        {SUBSCRIPTION}
        {", API-equivalent "}
        <Value leaf={apiEquivalent} />
      </>
    );
  }
  return (
    <Value leaf={cost} nullReason={cost.kind === "null" ? costReason(cost, reason) : undefined} />
  );
}

export function CostLine({ handlers, total, apiEquivalent }: Props) {
  const rows = rowsOf(handlers, (row) => `[${row}].handler`);

  if (total === undefined) {
    return (
      <span data-testid="cost-line">
        {rows.map((row) => (
          <CostFigure key={row} handlers={handlers} row={row} />
        ))}
      </span>
    );
  }

  return (
    <ul className={styles.rows} data-testid="cost-line-rows">
      {rows.map((row) => {
        const handler = handlers.at(`[${row}].handler`);
        return (
          <li key={row} className={styles.row}>
            <SourceChip label={handlerLabel(handler)} leaf={handler} />
            <span className={styles.figure}>
              <CostFigure handlers={handlers} row={row} />
            </span>
          </li>
        );
      })}
      <li className={styles.row}>
        <span className={styles.label}>{TOTAL_LABEL}</span>
        <span className={styles.figure}>
          <Value leaf={total} nullReason={nullReasonFor(total.route, total.path)} />
        </span>
      </li>
      {apiEquivalent === undefined ? null : (
        <li className={styles.row}>
          <span className={styles.label}>{API_EQUIVALENT_LABEL}</span>
          <span className={styles.figure}>
            <Value
              leaf={apiEquivalent}
              nullReason={nullReasonFor(apiEquivalent.route, apiEquivalent.path)}
            />
          </span>
        </li>
      )}
    </ul>
  );
}
