/**
 * The reply area for a finished message the store served no reply for, with the reason it gives
 * (ticket 219, `D-3`): a failed delegation or a raised run is a state of the reply, not a missing
 * surface, so it reads "No reply." and the served reason, never `AbsentState`'s 404 wording (which
 * is kept for a null reply with a null reason, a true wire gap).
 *
 * A delegation reason's status word is shown through `HUMAN_WORDS` where the table has it; the
 * ledger's own word stays in the title.
 */
import { humanWord } from "../lib/human-words";
import type { Leaf } from "../lib/wire";

import styles from "./ReplyAbsent.module.css";

/** `loop/messages.py::_reason_from_events`'s delegation sentence: agent, status, optional reason. */
const DELEGATION = /^the delegation to (\S+) (\S+?)(?:: (\S+))?$/;

type Props = {
  /** The served `reply_reason` leaf, whole. */
  reason: Leaf;
};

function shown(reason: string): string {
  const match = DELEGATION.exec(reason);
  if (match === null) return reason;
  const [, agent, status, detail] = match;
  const statusWord = humanWord("delegation_status", status as string) ?? status;
  if (detail === undefined) return `the delegation to ${agent}: ${statusWord}`;
  const detailWord = humanWord("delegation_reason", detail) ?? detail;
  return `the delegation to ${agent}: ${statusWord} (${detailWord})`;
}

export function ReplyAbsent({ reason: leaf }: Props) {
  const reason = leaf.text;
  // A reason the table words as a finish (`reply_reason`, ticket 273) is not a missing reply: it
  // reads as the task finishing, with no "No reply." above it.
  const finished = humanWord("reply_reason", reason);
  if (finished !== undefined) {
    return (
      <div className={styles.absent} role="status" data-testid="reply-finished">
        <p className={styles.headline} title={reason}>
          {finished}
        </p>
      </div>
    );
  }
  return (
    <div className={styles.absent} role="status" data-testid="reply-absent">
      <p className={styles.headline}>No reply.</p>
      <p className={styles.reason} title={reason}>
        {shown(reason)}
      </p>
    </div>
  );
}
