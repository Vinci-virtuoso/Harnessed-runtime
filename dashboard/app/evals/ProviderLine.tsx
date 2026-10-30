"use client";

/**
 * Which provider and model produced a run, as the gateway served them (FR-72, decision 40).
 *
 * Decision 40 shipped this as one line of static copy because no ledger recorded a provider.
 * Migration 010 records one, so it is a figure now: both fields are `Value`s read off the response
 * the panel fetched, mono, each with its route and path. Nothing here names a provider the wire
 * did not send.
 *
 * **Under `fake` the provider carries the Vision's caveat as its gloss.** The scores a replaying
 * fixture produces prove the mechanism and nothing about a model, and `improve promote` prints
 * the same sentence under `fake` and under no other provider; the gloss follows the same rule.
 *
 * A run opened before migration 010 sends `null` for both, which renders `unknown` with the
 * sentence `lib/null-reasons.ts` keys on the route and path, never a blank and never a default.
 */
import { Value } from "../../components/Value";
import type { Leaf, Wire } from "../../lib/wire";

import { reasonIf } from "./EvalRunsPanel";
import styles from "./evals.module.css";

/** The provider the gloss belongs to. `improve/promoter.py::FAKE`, as the wire spells it. */
export const FAKE_PROVIDER = "fake";

/** The Vision's caveat, as `improve/promoter.py::CAVEAT` says it after `provider=fake - `. */
export const FAKE_GLOSS = "this proves the promotion mechanism, not model quality";

type Props = {
  wire: Wire;
  /** The route the panel fetched, which the null reasons are keyed on. */
  route: string;
  /** The jq path of the object holding the two fields; the response's root by default. */
  at?: string;
};

export function ProviderLine({ wire, route, at = "" }: Props) {
  const provider = wire.at(`${at}.provider`);
  const model = wire.at(`${at}.model`);

  return (
    <p className={styles.provider} data-testid="provider-line">
      <span className={styles.providerField}>
        <span className={styles.providerLabel}>provider:</span>
        <Value leaf={provider} nullReason={reasonIf(provider, route, provider.path)} />
      </span>
      <span className={styles.providerField}>
        <span className={styles.providerLabel}>model:</span>
        <Value leaf={model} nullReason={reasonIf(model, route, model.path)} />
      </span>
      {isFake(provider) ? (
        <span className={styles.gloss} data-testid="provider-gloss">
          {FAKE_GLOSS}
        </span>
      ) : null}
    </p>
  );
}

/** Whether the served provider is the replaying fixture. A null is not `fake`; it is unknown. */
function isFake(leaf: Leaf): boolean {
  return leaf.kind === "string" && leaf.text === FAKE_PROVIDER;
}
