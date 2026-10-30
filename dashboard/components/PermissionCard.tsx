"use client";

/**
 * One agent request for permission, on the exchange it belongs to (FR-154, UJ-26, G-42, G-43,
 * G-55; design.md "PermissionCard"). Every state is a served field: the words come from
 * `lib/human-words.ts`, the times are the served `requested_at`, `ceiling_at` and `answered_at`
 * humanized, and nothing counts down - the ceiling is re-read against the dashboard clock once a
 * minute and no more (`D-1` as amended).
 *
 * **It never answers by accident.** `Approve` and `Deny` are real buttons in that tab order; neither
 * takes focus when the card arrives and neither is bound to Enter. A press posts through
 * `lib/control-client.ts` once, both buttons disable and the pressed one says what it is doing
 * until the answer lands. The card is a labelled `section`, not a live region: its arrival and its
 * answer are announced once each through the conversation's own polite region.
 */
import { useEffect, useId, useState } from "react";

import { STALE_TICK_MS, now } from "../lib/clock";
import {
  type ControlResult,
  answerPermission,
  permissionRoute,
  type PermissionAnswer,
} from "../lib/control-client";
import { humanWord } from "../lib/human-words";
import { humanCeiling, humanClock } from "../lib/humanize";
import { leafIfSent } from "../lib/rows";
import { type Leaf, type Wire, parseWire } from "../lib/wire";

import { CodeView } from "./CodeView";
import { ErrorState } from "./ErrorState";
import { UnreachableState } from "./UnreachableState";
import { Value } from "./Value";
import styles from "./PermissionCard.module.css";

export const APPROVE = "Approve";
export const DENY = "Deny";
export const APPROVING = "Approving...";
export const DENYING = "Denying...";

const PENDING = "pending";
const CONFLICT = "409";
const CONTROL_APP = "control app";

function lowerFirst(text: string): string {
  return `${text.charAt(0).toLowerCase()}${text.slice(1)}`;
}

function served(wire: Wire, path: string): Leaf | undefined {
  const leaf = leafIfSent(wire, path);
  return leaf === undefined || leaf.kind === "null" ? undefined : leaf;
}

/** "Claude Code asks to run a command": the agent and the tool kind's words, both served. */
export function requestSentence(permission: Wire, verb: string): string {
  const agent = served(permission, ".agent")?.text ?? "An agent";
  const kind = served(permission, ".tool_kind")?.text;
  const words = kind === undefined ? undefined : humanWord("tool_kind", kind);
  return `${agent} ${verb} ${words === undefined ? "do something" : lowerFirst(words)}`;
}

/** The sentence the conversation's polite region says when a card arrives or changes. */
export function announcement(permission: Wire): string {
  const state = permission.at(".state").text;
  if (state === PENDING) return requestSentence(permission, "asks to");
  const words = humanWord("permission_state", state) ?? state;
  return `${requestSentence(permission, "asked to")}: ${lowerFirst(words)}`;
}

/** "Already answered: approved from the terminal at 03:15", from the 409 body alone. */
function alreadyAnswered(result: Extract<ControlResult, { state: "error" }>): string {
  try {
    const body = parseWire(result.response.text, permissionRoute(""));
    const answer = served(body, ".answer");
    const via = served(body, ".answered_via");
    const at = served(body, ".answered_at");
    const parts = [
      answer === undefined ? undefined : lowerFirst(humanWord("permission_answer", answer.text) ?? answer.text),
      via === undefined ? undefined : (humanWord("answered_via", via.text) ?? via.text),
      at === undefined ? undefined : `at ${humanClock(at).text}`,
    ].filter((part) => part !== undefined);
    return parts.length === 0 ? `Already answered: ${result.message}` : `Already answered: ${parts.join(" ")}`;
  } catch {
    // Not decision 24's own body: the control app's words are the news, verbatim.
    return `Already answered: ${result.message}`;
  }
}

type Props = {
  /** One row of `GET /v1/permissions?session=`, scoped to this decision (decision 42). */
  permission: Wire;
  /** Called once an answer is accepted or found already given, so the caller reads the decision again. */
  onAnswered: () => void;
};

export function PermissionCard({ permission, onAnswered }: Props) {
  const titleId = useId();
  const state = permission.at(".state");
  const pending = state.text === PENDING;
  const description = served(permission, ".description");
  const detail = served(permission, ".detail");
  const warning = served(permission, ".warning");
  const requestedAt = permission.at(".requested_at");
  const ceilingAt = served(permission, ".ceiling_at");
  const answeredAt = served(permission, ".answered_at");
  const permissionId = permission.at(".permission_id");

  // A fresh object once a minute: the ceiling's "(in 8 min)" re-reads the dashboard clock, nothing ticks.
  const [, setTick] = useState<object>({});
  useEffect(() => {
    if (!pending) return undefined;
    const interval = setInterval(() => setTick({}), STALE_TICK_MS);
    return () => clearInterval(interval);
  }, [pending]);

  const [answering, setAnswering] = useState<PermissionAnswer | undefined>(undefined);
  const [already, setAlready] = useState<string | undefined>(undefined);
  const [refused, setRefused] = useState<Exclude<ControlResult, { state: "ok" }> | undefined>(undefined);

  async function answer(choice: PermissionAnswer): Promise<void> {
    if (answering !== undefined) return;
    setAnswering(choice);
    setAlready(undefined);
    setRefused(undefined);
    const result = await answerPermission(permissionId.text, choice);
    if (result.state === "ok") {
      // The card stays as it is, disabled, until the decision is read again and says what happened.
      onAnswered();
      return;
    }
    setAnswering(undefined);
    if (result.state === "error" && result.response.status === CONFLICT) {
      setAlready(alreadyAnswered(result));
      onAnswered();
      return;
    }
    setRefused(result);
  }

  const title = requestSentence(permission, pending ? "asks to" : "asked to");

  return (
    <section
      className={styles.card}
      data-state={state.text}
      aria-labelledby={titleId}
      data-testid="permission-card"
    >
      <h3 id={titleId} className={styles.title}>
        {title}
      </h3>
      {description === undefined ? null : <CodeView output={description} />}
      {detail === undefined ? null : (
        <details className={styles.detail} data-testid="permission-detail">
          <summary>What the agent sent</summary>
          <CodeView output={detail} />
        </details>
      )}
      <p className={styles.when}>{pending ? <Asked requestedAt={requestedAt} ceilingAt={ceilingAt} /> : <Settled state={state} ceilingAt={ceilingAt} answeredAt={answeredAt} />}</p>
      {warning === undefined ? null : (
        <p className={styles.warning} data-testid="permission-warning">
          <Value leaf={warning} />
        </p>
      )}
      {pending ? (
        <>
          <div className={styles.controls}>
            <button
              type="button"
              className={styles.approve}
              disabled={answering !== undefined}
              onClick={() => void answer("approve")}
            >
              {answering === "approve" ? APPROVING : APPROVE}
            </button>
            <button
              type="button"
              className={styles.deny}
              disabled={answering !== undefined}
              onClick={() => void answer("deny")}
            >
              {answering === "deny" ? DENYING : DENY}
            </button>
          </div>
          <p className={styles.closing}>
            {`Approving lets ${served(permission, ".agent")?.text ?? "the agent"} do this one thing. Harnessed never pushes, merges or deploys.`}
          </p>
        </>
      ) : null}
      {already === undefined ? null : (
        <p className={styles.already} data-testid="already-answered">
          {already}
        </p>
      )}
      {refused === undefined ? null : refused.state === "unreachable" ? (
        <UnreachableState cause={refused.cause} />
      ) : (
        <ErrorState
          status={refused.response.status}
          error={refused.error}
          message={refused.message}
          answeredBy={CONTROL_APP}
        />
      )}
    </section>
  );
}

/** "Asked 03:12. Denied automatically at 03:22 (in 8 min) if you do not answer." */
function Asked({ requestedAt, ceilingAt }: { requestedAt: Leaf; ceilingAt: Leaf | undefined }) {
  return (
    <>
      Asked <Value leaf={requestedAt} human={humanClock(requestedAt)} />.
      {ceilingAt === undefined ? null : (
        <>
          {" Denied automatically at "}
          <Value leaf={ceilingAt} human={humanCeiling(ceilingAt, now())} />
          {" if you do not answer."}
        </>
      )}
    </>
  );
}

/** What became of a request nobody is waiting on any more, each in its own served words. */
function Settled({
  state,
  ceilingAt,
  answeredAt,
}: {
  state: Leaf;
  ceilingAt: Leaf | undefined;
  answeredAt: Leaf | undefined;
}) {
  if (state.text === "approved" || state.text === "denied") {
    const verb = state.text === "approved" ? "Approved" : "Denied";
    return (
      <>
        {`${verb} by you`}
        {answeredAt === undefined ? null : (
          <>
            {" at "}
            <Value leaf={answeredAt} human={humanClock(answeredAt)} />
          </>
        )}
      </>
    );
  }
  if (state.text === "expired" && ceilingAt !== undefined) {
    return (
      <>
        {"No answer by "}
        <Value leaf={ceilingAt} human={humanClock(ceilingAt)} />
        {", so it was denied"}
      </>
    );
  }
  return <>{humanWord("permission_state", state.text) ?? state.text}</>;
}
