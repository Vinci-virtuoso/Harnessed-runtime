"use client";

/**
 * One declared agent's card: its registry fields and its newest recorded handshake probe, read
 * and never run (FR-121, UJ-17, UJ-21; design.md "RegistryCard"). Replaces `AgentCard` - the
 * probe badge is kept, and every registry field `GET /v1/agents` serves from v5 joins it: human
 * words for `kind` and `billing`, `use_for` and `not_for` verbatim, and `confined`.
 *
 * `entry` is a `Wire` scoped to one row of `/v1/agents` (a `Wire` subtree travels like a `Leaf`,
 * decision 42), so every field below is read through `entry.at(...)` rather than a prop per
 * field.
 *
 * **No button runs anything.** Probing an agent CLI is not one of the control app's routes, so
 * the card's only affordance is `D-8`'s "copy, not act": one `harnessed agents test <name>` line.
 * The name is the row's `agent` verbatim (MUST rule 25).
 */
import { humanWord } from "../lib/human-words";
import { nullReasonFor } from "../lib/null-reasons";
import type { Leaf, Wire } from "../lib/wire";

import { Value } from "./Value";
import styles from "./RegistryCard.module.css";

/** The four words the probe badge can read, strongest first (unchanged from `AgentCard`). */
export const REGISTRY_WORDS = ["live", "stale", "failed", "unproven"] as const;
export type RegistryWord = (typeof REGISTRY_WORDS)[number];

/** What the one control says. It copies; it does not run anything. */
export const COPY_LABEL = "Copy command";

/** design.md's own sentence for an empty `use_for`: the router has nothing to route this on. */
export const NO_USE_FOR = "No use_for: the router has nothing to route on";
/** The visible gloss a `cli` entry's "Unconfined" tag carries beside it (design.md verbatim). */
export const UNCONFINED_GLOSS =
  "runs in its working directory only; nothing stronger stops it";
export const UNCONFINED = "Unconfined";
export const NOT_FOR_LABEL = "Not for:";

/** The confinement line's words (FR-131, G-60), one per served `settings` / `jail` value. */
export const SETTINGS_HARNESSED_LINE = "Runs with Harnessed's settings";
export const SETTINGS_FOLDER_LINE = "Runs with its folder's settings";
export const JAIL_JOB_OBJECT_LINE =
  "Jailed: Job Object, limits its processes only";
export const JAIL_BWRAP_LINE = "Jailed: bwrap";
export const JAIL_NONE_LINE = "Unconfined: bwrap could not start here";

const TRUE = "true";
const PASSED = "passed";

type Props = {
  /** One row of `GET /v1/agents`, scoped to this entry - never the whole response. */
  entry: Wire;
};

/** The card's badge word, read off the row: `live` only on a pass that still matches. */
export function registryWord(
  status: Leaf,
  fingerprintMatches: Leaf,
  probed: Leaf,
): RegistryWord {
  if (probed.text !== TRUE) return "unproven";
  if (fingerprintMatches.text !== TRUE) return "stale";
  return status.text === PASSED ? "live" : "failed";
}

/**
 * The one confinement line, read from the three served fields and nothing else: a null `settings`
 * is a `cli` entry (its `jail` speaks), a null `jail` an `acp` entry (its `settings` speaks), and
 * `confined_reason` is carried verbatim. `warn` is the unconfined case.
 */
export function confinementLine(
  settings: Leaf,
  jail: Leaf,
  reason: Leaf,
): { text: string; warn: boolean } {
  const why = reason.kind === "null" ? "" : reason.text;
  if (settings.kind !== "null") {
    if (settings.text === "harnessed")
      return { text: SETTINGS_HARNESSED_LINE, warn: false };
    return {
      text:
        why === "" ? SETTINGS_FOLDER_LINE : `${SETTINGS_FOLDER_LINE}: ${why}`,
      warn: false,
    };
  }
  if (jail.text === "job_object")
    return { text: JAIL_JOB_OBJECT_LINE, warn: false };
  if (jail.text === "bwrap") return { text: JAIL_BWRAP_LINE, warn: false };
  return {
    text: why === "" ? JAIL_NONE_LINE : `${JAIL_NONE_LINE} (${why})`,
    warn: true,
  };
}

export function RegistryCard({ entry }: Props) {
  const agent = entry.at(".agent");
  const status = entry.at(".status");
  const fingerprintMatches = entry.at(".fingerprint_matches");
  const probed = entry.at(".probed");
  const kind = entry.at(".kind");
  const billing = entry.at(".billing");
  const useFor = entry.at(".use_for");
  const notFor = entry.at(".not_for");
  const confined = entry.at(".confined");
  const line = confinementLine(
    entry.at(".settings"),
    entry.at(".jail"),
    entry.at(".confined_reason"),
  );

  const word = registryWord(status, fingerprintMatches, probed);
  const kindWord = humanWord("kind", kind.text);
  const kindHuman =
    kindWord === undefined ? undefined : { text: kindWord, exact: kind.text };
  const billingWord =
    billing.kind === "null" ? undefined : humanWord("billing", billing.text);
  const billingHuman =
    billingWord === undefined
      ? undefined
      : { text: billingWord, exact: billing.text };
  const command = `harnessed agents test ${agent.text}`;

  return (
    <section
      className={styles.card}
      aria-label={agent.text}
      data-testid="registry-card"
    >
      <div className={styles.header}>
        <h3 className={styles.name}>{agent.text}</h3>
        <span
          className={`${styles.badge} ${styles[word]}`}
          data-testid="probe-badge"
          data-word={word}
        >
          {word}
        </span>
      </div>
      <p className={styles.tags}>
        <span className={styles.tag}>
          <Value leaf={kind} human={kindHuman} />
        </span>
        {confined.text === "false" ? (
          <span className={`${styles.tag} ${styles.warn}`}>{UNCONFINED}</span>
        ) : null}
        <span className={styles.tag}>
          <Value
            leaf={billing}
            human={billingHuman}
            nullReason={nullReasonFor(billing.route, billing.path)}
          />
        </span>
      </p>
      <p
        className={line.warn ? styles.warnLine : styles.gloss}
        data-testid="confinement-line"
      >
        {line.text}
      </p>
      {confined.text === "false" ? (
        <p className={styles.gloss}>{UNCONFINED_GLOSS}</p>
      ) : null}
      {useFor.text === "" ? (
        <p className={styles.warnLine}>
          {NO_USE_FOR}
          <code className={styles.code} data-testid="doctor-command">
            harnessed doctor
          </code>
          <button
            type="button"
            className={styles.copy}
            onClick={() => copy("harnessed doctor")}
          >
            {COPY_LABEL}
          </button>
        </p>
      ) : (
        <p className={styles.useFor}>
          <Value leaf={useFor} />
        </p>
      )}
      {notFor.text === "" ? null : (
        <p className={styles.notFor}>
          {NOT_FOR_LABEL} <Value leaf={notFor} />
        </p>
      )}
      <div className={styles.command}>
        <code className={styles.code} data-testid="agent-test-command">
          {command}
        </code>
        <button
          type="button"
          className={styles.copy}
          onClick={() => copy(command)}
        >
          {COPY_LABEL}
        </button>
      </div>
    </section>
  );
}

/** Put the line on the clipboard. A browser that refuses simply leaves the text to be selected. */
function copy(line: string): void {
  void globalThis.navigator?.clipboard?.writeText(line);
}
