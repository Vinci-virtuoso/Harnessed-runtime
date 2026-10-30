"use client";

/**
 * One provider's card and the `CredentialField` it holds (FR-88, FR-89, UJ-17; design.md "Provider
 * cards"). The card renders one row of `GET /v1/providers` as the gateway states it (ticket 147):
 * the name, `configured` as a `PassMark` printing the verbatim field (never "connected" or
 * "active", `D-7`), and `key_var` and `default_model` as `Value`s in mono. An unconfigured card
 * holds its field open; a configured one keeps it behind a "rotate key" text action, because a
 * configured credential is not something to invite re-entering casually; a provider whose
 * `key_var` is `null` takes no key and has no field. There is no pin, star, "make default" or
 * model-switch control: FR-89 rejects all four, and the field is the card's only write.
 *
 * **The credential travels in a JSON body and nowhere else**: `configureProvider` in
 * `lib/control-client.ts`, the one door that writes (MUST rule 22), posts `{provider, credential}`
 * to `/control/v1/providers`. Never a query string, never a path segment - the one absolute rule
 * of this page. The form is `autocomplete="off"`, the field is `type="password"`, the typed value
 * lives in this component's state and in no browser storage (decision 32), and the field clears
 * itself on any answer, success, refusal or none, before the answer is read.
 *
 * A refusal is the control app's own body, quoted: `CredentialRefused` carries the refused
 * status's word and never the provider SDK's message, which the control app drops (`D-7`).
 */
import { type FormEvent, useId, useState } from "react";

import { type ControlResult, configureProvider } from "../lib/control-client";
import { nullReasonFor } from "../lib/null-reasons";
import type { Leaf } from "../lib/wire";

import { ErrorState } from "./ErrorState";
import { PassMark } from "./PassMark";
import { UnreachableState } from "./UnreachableState";
import { Value } from "./Value";
import styles from "./ProviderCard.module.css";

export const SAVE = "Save key";
export const SAVING = "Saving…";
/** What `saving` announces, politely (design.md: `saving` announces). */
export const SAVING_ANNOUNCEMENT = "Checking the key with one call to the provider";
/** What a `200` announces. The key itself is never read back, so there is nothing else to say. */
export const SAVED_ANNOUNCEMENT = "Key checked and saved to .env";

/** The configured card's one affordance: a text action that re-opens the field for rotation. */
export const ROTATE_KEY = "rotate key";

/** How the wire spells a true boolean; this tree compares characters, never parsed values. */
const TRUE = "true";

/** Who answers a refusal: the control app, never the read gateway. */
const CONTROL_APP = "control app";

/** The field's visible label: text, never a placeholder (design.md). */
export function credentialLabel(provider: string): string {
  return `API key for ${provider}`;
}

type Props = {
  /** The row's `name`: the card's title, and the provider the field's key is for. */
  name: Leaf;
  /** The row's `configured`: whether the key variable is set, by name only. */
  configured: Leaf;
  /** The row's `key_var`, `null` for a provider that takes no key. */
  keyVar: Leaf;
  /** The row's `default_model`, `null` where the table knows no default. */
  defaultModel: Leaf;
  /** Called with every answer, so the page can read the provider state again after a save. */
  onAnswered?: (result: ControlResult) => void;
};

/** A save that did not configure the provider: the control app's refusal, or no answer. */
type Refused = Exclude<ControlResult, { state: "ok" }>;

export function ProviderCard({ name, configured, keyVar, defaultModel, onAnswered }: Props) {
  const [rotating, setRotating] = useState(false);
  const id = useId();
  const keyed = keyVar.kind !== "null";
  const collapsible = keyed && configured.text === TRUE;
  const open = keyed && (!collapsible || rotating);

  return (
    <section className={styles.card} aria-labelledby={`${id}-name`} data-testid="provider-card">
      {/* A `div`, not a `header`: the page's one `header` is the top bar with the gateway pill. */}
      <div className={styles.header}>
        <h3 id={`${id}-name`} className={styles.name}>
          {name.text}
        </h3>
        <PassMark leaf={configured} />
      </div>
      <dl className={styles.facts}>
        <Fact term="key_var" leaf={keyVar} />
        <Fact term="default_model" leaf={defaultModel} />
      </dl>
      {collapsible ? (
        <button
          type="button"
          className={styles.rotate}
          aria-expanded={rotating}
          onClick={() => setRotating(!rotating)}
        >
          {ROTATE_KEY}
        </button>
      ) : null}
      {open ? <CredentialField provider={name} onAnswered={onAnswered} /> : null}
    </section>
  );
}

/** One field of the row, named as the wire names it, with the reason where the wire sent null. */
function Fact({ term, leaf }: { term: string; leaf: Leaf }) {
  return (
    <div className={styles.fact}>
      <dt className={styles.term}>{term}</dt>
      <dd className={styles.value}>
        <Value leaf={leaf} nullReason={nullReasonFor(leaf.route, leaf.path)} />
      </dd>
    </div>
  );
}

type FieldProps = {
  /** The row's `name`, as a leaf: the provider the key is for, verbatim from `GET /v1/providers`. */
  provider: Leaf;
  onAnswered?: (result: ControlResult) => void;
};

/** The form (ticket 146): one password field, posted as a JSON body, cleared on any answer. */
export function CredentialField({ provider, onAnswered }: FieldProps) {
  const [credential, setCredential] = useState("");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [refused, setRefused] = useState<Refused | undefined>(undefined);
  const id = useId();

  async function onSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (credential === "" || saving) return;
    setSaving(true);
    setSaved(false);
    setRefused(undefined);
    const result = await configureProvider(provider.text, credential);
    // Any answer clears the field, before the answer is read: the value is gone either way.
    setCredential("");
    setSaving(false);
    if (result.state === "ok") setSaved(true);
    else setRefused(result);
    onAnswered?.(result);
  }

  return (
    <form className={styles.field} autoComplete="off" onSubmit={(event) => void onSubmit(event)}>
      <label className={styles.label} htmlFor={`${id}-credential`}>
        {credentialLabel(provider.text)}
      </label>
      <div className={styles.row}>
        <input
          id={`${id}-credential`}
          className={styles.input}
          type="password"
          autoComplete="off"
          spellCheck={false}
          value={credential}
          disabled={saving}
          onChange={(event) => setCredential(event.target.value)}
        />
        <button type="submit" className={styles.save} disabled={credential === "" || saving}>
          {saving ? SAVING : SAVE}
        </button>
      </div>
      <p className={styles.status} role="status" aria-live="polite">
        {saving ? SAVING_ANNOUNCEMENT : saved ? SAVED_ANNOUNCEMENT : ""}
      </p>
      {refused === undefined ? null : <Refusal refused={refused} />}
    </form>
  );
}

/** A save the control app refused, quoted; or one it never answered (MUST rule 23). */
function Refusal({ refused }: { refused: Refused }) {
  if (refused.state === "unreachable") {
    return <UnreachableState cause={refused.cause} />;
  }
  return (
    <ErrorState
      status={refused.response.status}
      error={refused.error}
      message={refused.message}
      answeredBy={CONTROL_APP}
    />
  );
}
