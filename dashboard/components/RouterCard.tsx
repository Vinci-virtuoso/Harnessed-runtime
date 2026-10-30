"use client";

/**
 * The router's newest recorded live probe, and every route's confidence floor (FR-121, UJ-17,
 * UJ-21; design.md "RouterCard", decision 67, G-32). First in the Models page's "Router and
 * agents" grid, at `#router` - the anchor the Overview diagram's delegation box links to.
 *
 * `router` is the whole `/v1/router` response as a `Wire` (a `Wire` subtree travels like a
 * `Leaf`, decision 42). **0** figures here come from `doctor --json` in the browser: every field
 * is this route's own, never a second read.
 */
import { humanWord } from "../lib/human-words";
import { rowsOf } from "../lib/rows";
import { nullReasonFor } from "../lib/null-reasons";
import type { Leaf, Wire } from "../lib/wire";

import { now } from "../lib/clock";
import { humanAgo, humanSecondsWords, type Human } from "../lib/humanize";
import { Value } from "./Value";
import styles from "./RouterCard.module.css";

/** The three words the card's own badge can read. */
export const ROUTER_WORDS = ["probed", "failed", "never probed"] as const;
export type RouterWord = (typeof ROUTER_WORDS)[number];

/** What the one control says. It copies; it does not run anything. */
export const COPY_LABEL = "Copy command";

/** The live rung's own command line, exactly as `doctor_cmd.py`'s rung 5 detail spells it. */
export const LIVE_RUNG_COMMAND = "harnessed doctor --live";

const TRUE = "true";
const PASSED = "passed";

/** The badge's word, read off the served fields: `probed` only on a pass. */
export function routerWord(probed: Leaf, status: Leaf): RouterWord {
  if (probed.text !== TRUE) return "never probed";
  return status.text === PASSED ? "probed" : "failed";
}

type Props = {
  /** `GET /v1/router`'s response, whole. */
  router: Wire;
};

export function RouterCard({ router }: Props) {
  const model = router.at(".model");
  const probed = router.at(".probed");
  const status = router.at(".status");
  const latencySeconds = router.at(".latency_seconds");
  const cost = router.at(".cost");
  const detail = router.at(".detail");
  const probedAt = router.at(".probed_at");
  const needsMemoryThresholdPercent = router.at(".needs_memory_threshold_percent");

  const routing = router.at(".routing");
  const routingReason = router.at(".routing_reason");

  const word = routerWord(probed, status);
  const floors = rowsOf(router, (index) => `.floors[${index}].route`);

  // No `aria-label`: `RouterPanel`'s own `PanelFrame` region is already named "Router" (its
  // title), and one card per panel means a second, identically-named nested landmark would be a
  // pure duplicate rather than a real distinction (unlike `RegistryCard`, whose `aria-label`
  // names one row among several under the shared "Agents" panel).
  return (
    <section id="router" className={styles.card} data-testid="router-card">
      <div className={styles.header}>
        <span className={`${styles.badge} ${styles[wordClass(word)]}`} data-testid="router-probe-badge" data-word={word}>
          {word}
        </span>
      </div>
      <p className={styles.detail} data-testid="router-routing">
        <Value leaf={routing} human={routingHuman(routing)} />
        {routingReason.kind === "null" ? null : (
          <>
            {" - "}
            <Value leaf={routingReason} nullReason={nullReasonFor(routingReason.route, routingReason.path)} />
          </>
        )}
      </p>
      <p className={styles.model}>
        <span className={styles.label}>Jev</span>
        <Value leaf={model} nullReason={nullReasonFor(model.route, model.path)} />
      </p>
      <dl className={styles.facts}>
        <Fact term="latency_seconds" leaf={latencySeconds} human={latencySeconds.kind === "null" ? undefined : humanSecondsWords(latencySeconds)} />
        <Fact term="cost" leaf={cost} />
        <Fact term="probed_at" leaf={probedAt} human={probedAt.kind === "null" ? undefined : humanAgo(probedAt, now())} />
      </dl>
      <p className={styles.detail}>
        <Value leaf={detail} nullReason={nullReasonFor(detail.route, detail.path)} />
      </p>
      <div>
        <p className={styles.floorsHeading}>Confidence floors</p>
        <ul className={styles.floors}>
          {floors.map((row) => {
            const routeLeaf = router.at(`.floors[${row}].route`);
            const percent = router.at(`.floors[${row}].floor_percent`);
            return (
              <li key={row} className={styles.floor}>
                <span className={styles.floorRoute}>
                  <Value leaf={routeLeaf} />
                </span>
                <Value leaf={percent} />
              </li>
            );
          })}
        </ul>
        <p className={styles.gloss}>
          needs-memory threshold: <Value leaf={needsMemoryThresholdPercent} />
        </p>
      </div>
      <div className={styles.command}>
        <code className={styles.code} data-testid="router-live-command">
          {LIVE_RUNG_COMMAND}
        </code>
        <button type="button" className={styles.copy} onClick={() => copy(LIVE_RUNG_COMMAND)}>
          {COPY_LABEL}
        </button>
      </div>
    </section>
  );
}

/** "Routing is on" / "Routing is off", from `human-words.ts` (ticket 317, decision 105). */
function routingHuman(routing: Leaf): Human | undefined {
  const text = humanWord("routing", routing.text);
  return text === undefined ? undefined : { text, exact: routing.text };
}

/** One field of the row, named as the wire names it, with the reason where the wire sent null. */
function Fact({ term, leaf, human }: { term: string; leaf: Leaf; human?: Human }) {
  return (
    <div className={styles.fact}>
      <dt className={styles.term}>{term}</dt>
      <dd className={styles.value}>
        <Value leaf={leaf} human={human} nullReason={nullReasonFor(leaf.route, leaf.path)} />
      </dd>
    </div>
  );
}

/** The badge word, as a CSS Modules identifier: "never probed" has a space, class names do not. */
function wordClass(word: RouterWord): "probed" | "failed" | "neverProbed" {
  if (word === "never probed") return "neverProbed";
  return word;
}

/** Put the line on the clipboard. A browser that refuses simply leaves the text to be selected. */
function copy(line: string): void {
  void globalThis.navigator?.clipboard?.writeText(line);
}
