"use client";

/**
 * `/evals` - the eval runs, their comparison and the improvement history, stacked on one page
 * (FR-45, FR-56, FR-57, FR-64, FR-129; UJ-8, UJ-9). `?id=` puts one eval run at the top,
 * `?before=&after=` fills the Compare section, `?cycle=` fills the Improvement section, and one
 * `?limit=` drives both listings.
 *
 * A client component holding no data (decision 34): it reads the query and nothing else, and
 * every figure below it is fetched in the browser, so no gateway value is ever in the
 * server-rendered payload. **The parameter absent renders the listing, present renders the
 * detail** (decision 45): the id rides in `?id=` rather than a path segment, read by the one
 * mechanism this page already had for `?limit=`.
 *
 * The `?limit=` the URL carries is passed down as its characters, unvalidated: whether a limit is
 * servable is the gateway's answer to give, and its `LimitError` is what an operator sees when it
 * will not serve one. The id is passed down exactly as the URL carried it - no trim, no re-casing,
 * no split - because the gateway is the only thing that takes an id apart (MUST rule 25), and an
 * id the ledgers never held arrives as the panel's 404 quoting the gateway's own body.
 *
 * Decision 40's static line is gone (FR-72): migration 010 records a provider, so which provider
 * produced these scores is a served figure on each run, in `ProviderLine` and the listing's
 * provider column, never copy on the page.
 */
import { useSearchParams } from "next/navigation";

import { CliCommand } from "./CliCommand";
import { ComparePanel } from "./ComparePanel";
import { CycleLookup } from "./CycleLookup";
import { CyclePanel } from "./CyclePanel";
import { EvalRunPanel } from "./EvalRunPanel";
import { EvalRunsPanel } from "./EvalRunsPanel";
import { PromotionsPanel } from "./PromotionsPanel";
import { WorkSuitesPanel } from "./WorkSuitesPanel";
import { DEFAULT_LIMIT, PROMOTE_COMMAND, RUN_JSON_COMMAND } from "./limit";
import improve from "./improve.module.css";
import styles from "./evals.module.css";

/** Why the promote command is offered here rather than a button (`D-8`). */
export const PROMOTE_NOTE = "promoting is the CLI's, not this page's:";

/** Where the id the lookup wants comes from, since no listing offers one. */
export const LOOKUP_NOTE = "there is no listing of cycles; this id is printed by";

export default function EvalsPage() {
  // The URL is read here, in the browser, because an export has no server to read it on
  // (decision 45); each value is the characters the URL carried, decoded once and never
  // trimmed, split or reformatted (MUST rule 25).
  const query = useSearchParams();
  const limit = query.get("limit") ?? undefined;
  const id = query.get("id") ?? undefined;
  const before = query.get("before") ?? undefined;
  const after = query.get("after") ?? undefined;
  const cycle = query.get("cycle") ?? undefined;

  return (
    <div className={styles.page}>
      <h1 className={styles.title}>Evals</h1>
      <p className={styles.subtitle}>
        Every eval run the ledger recorded, the comparison of any two, and every promotion the
        promoter decided.
      </p>
      {id !== undefined && <EvalRunPanel evalRunId={id} />}
      <WorkSuitesPanel />
      <EvalRunsPanel limit={limit ?? DEFAULT_LIMIT} />
      <section id="compare" className={styles.section} aria-label="Compare">
        <h2 className={styles.sectionTitle}>Compare</h2>
        <p className={styles.note}>
          The comparison the gateway serves for two eval run ids, as sent: which cases moved, which
          are new and which are missing. Nothing here is subtracted.
        </p>
        <ComparePanel before={before} after={after} />
      </section>
      <section id="improvement" className={styles.section} aria-label="Improvement">
        <h2 className={styles.sectionTitle}>Improvement</h2>
        <p className={styles.note}>
          Every promotion the promoter decided, as it decided it, and the cycle behind any one of
          them.
        </p>
        <PromotionsPanel limit={limit ?? DEFAULT_LIMIT} />
        <section className={improve.lookup} aria-label="Open an improvement cycle">
          <CliCommand note={LOOKUP_NOTE} command={RUN_JSON_COMMAND} />
          <CycleLookup />
        </section>
        {cycle !== undefined && <CyclePanel improvementRunId={cycle} />}
        <CliCommand note={PROMOTE_NOTE} command={PROMOTE_COMMAND} />
      </section>
    </div>
  );
}
