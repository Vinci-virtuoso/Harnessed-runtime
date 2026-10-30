"use client";

/**
 * One served message, rendered whole: the operator's text, the decisions that routed it, the
 * reply by kind, its caption, and a collapsed Steps disclosure (FR-122, FR-128, UJ-16, UJ-18 to
 * UJ-22; design.md "The console in v5", decision 66).
 *
 * `decisions` is this exchange's own slice of `/v1/sessions/{id}/decisions` (`Conversation`'s
 * read, sliced on the served `message_seq`), so its figures are sourced to that response rather
 * than to `message`'s - `ResponseSource` re-opens that context for exactly the decision-derived
 * figures, and `message`'s own figures (the reply, the caption) stay under the ambient messages
 * context `Conversation` already provides.
 *
 * **Steps, bounded without inferring them:** the collapsed summary reads the message's own served
 * `steps` count and `hops` outputs - both scoped to this `message_seq` by the store
 * (`loop/messages.py::_steps_for`, `_hops_for`), never a client-side filter of the events stream.
 * The live exchange additionally mounts `TraceTimeline` for its running trace, open while `live`
 * and collapsed once it finishes (`ux/experience.md`'s v5 addition); a byte-bounded replay of a
 * *finished* exchange's own `[first_seq, last_seq]` window is left to a later ticket (this ticket's
 * "For the architecture node" note), since neither `ReplayPanel` nor `StreamPanel` reads a bounded
 * window today and the served `steps`/`hops` figures already satisfy "not inferred" for a summary.
 */
import { useId, useState } from "react";

import { humanClock } from "../lib/humanize";
import { leafIfSent, rowsOf } from "../lib/rows";
import type { Leaf, Wire } from "../lib/wire";

import { AbsentState } from "./AbsentState";
import { DirtyTreeRefusal } from "./DirtyTreeRefusal";
import { FallbackNotice } from "./FallbackNotice";
import { OutputView } from "./OutputView";
import { PermissionCard } from "./PermissionCard";
import { RepoRecord } from "./RepoRecord";
import { ReplyAbsent } from "./ReplyAbsent";
import { type PanelContextValue, ResponseSource } from "./PanelFrame";
import { ReplyCaption } from "./ReplyCaption";
import { RouteLine } from "./RouteLine";
import { TraceTimeline } from "./TraceTimeline";
import { UncertainStop } from "./UncertainStop";
import { Value } from "./Value";
import styles from "./Exchange.module.css";

/** Who wrote a message, from the served `author` and `task_message` (FR-154, MUST rule 40). */
const AUTHOR_OPERATOR = "operator";
const TASK_FIRST = "first";
const TASK_CONTINUATION = "continuation";
export const LABEL_YOU = "You";
export const LABEL_FROM_TASK = "From the task";
export const LABEL_NEXT_STEP = "Next step of the task";

/** A hop the delegation refused for an unclean tree (`acp/ledger.py::DIRTY_TREE`). */
const HOP_FAILED = "failed";
const REASON_DIRTY_TREE = "dirty_tree";
const PENDING = "pending";
/** `router/route.py`'s chain terminal: not an agent. */
const ROUTE_DONE = "done";

const OUTCOME_UNCERTAIN = "uncertain";
const OUTCOME_FALLBACK = "fallback";
const OUTCOME_ROUTED = "routed";

/** No source for `error.raised`'s own detail exists on the messages read (that is the stream's
 * job, kept by `ConsolePanel`'s own `RaisedState`): this is MUST rule 27's defensive branch alone,
 * for a message the store somehow recorded with neither a turn nor an agent turn behind it. The
 * hop-0 uncertain stop is not that case: its served null has a documented reason, and the reply
 * area renders nothing for it (`UncertainStop` is the whole story). */
const NO_REPLY_ERROR = "NoReply";
const NO_REPLY_MESSAGE_FALLBACK = "no turn or agent turn is recorded behind this reply";

type Props = {
  /** One row of `/v1/sessions/{id}/messages`, scoped to this message (decision 42). */
  message: Wire;
  /** This message's own decisions, sliced from the session's decisions read, in served order. */
  decisions: readonly Wire[];
  /** Whether this is the newest, still-running message: `last_seq` not yet served. */
  live: boolean;
  /** The decisions response's own panel context, so a decision-derived figure cites it and not
   * the messages response `message` itself was read from. */
  decisionsPanel: PanelContextValue;
  /** The session this exchange belongs to, for the live exchange's own `TraceTimeline`. */
  sessionId: string;
  /** The previous message's served `last_seq` leaf, `undefined` for the session's first message. */
  afterSeq?: Leaf;
  /** This message's own permission decisions, in served order (FR-154); absent outside the console's read. */
  permissions?: readonly Wire[];
  /** The permissions response's own panel context, so a card's figures cite it. */
  permissionsPanel?: PanelContextValue;
  /** Called once a card's answer is accepted, so the decision is read again. */
  onAnswered?: () => void;
  /** Requests a stream frame placed here whose figures the permissions read has not returned yet. */
  arriving?: readonly string[];
  /** The task this message belongs to and its served working directory, where known. */
  taskId?: Leaf;
  taskCwd?: Leaf;
  /** The task's served entry agent, when it named one: who "did not start" in a dirty-tree refusal. */
  taskEntry?: Leaf;
  /** The task's served `created_at`: the first message reads "From the task, queued HH:MM" from it. */
  taskQueued?: Leaf;
  /** Whether the task is paused on an uncertain stop and this is the session's last message (FR-154). */
  pausedUncertain?: boolean;
};

function noop(): void {}

/** `wire`, scoped to one row's prefix: a `Wire` subtree travels like a `Leaf` (decision 42). */
function rowWire(wire: Wire, prefix: string): Wire {
  return {
    route: wire.route,
    text: wire.text,
    byteLength: wire.byteLength,
    at: (path: string) => wire.at(`${prefix}${path}`),
  };
}

/** The label of who wrote this message, or `undefined` for a message that served no `author`. */
function authorLabel(message: Wire, queued: Leaf | undefined): string | undefined {
  const author = leafIfSent(message, ".author");
  if (author === undefined) return undefined;
  if (author.text === AUTHOR_OPERATOR) return LABEL_YOU;
  const kind = leafIfSent(message, ".task_message");
  if (kind?.text === TASK_FIRST) {
    return queued === undefined || queued.kind === "null"
      ? LABEL_FROM_TASK
      : `${LABEL_FROM_TASK}, queued ${humanClock(queued).text}`;
  }
  if (kind?.text === TASK_CONTINUATION) return LABEL_NEXT_STEP;
  return LABEL_FROM_TASK;
}

function definedLeaf(leaf: Leaf): Leaf | undefined {
  return leaf.kind === "null" ? undefined : leaf;
}

function outputTextOrNull(leaf: Leaf): Leaf | null {
  return leaf.kind === "null" ? null : leaf;
}

/** The DOM anchor of one exchange: the served `message_seq`, verbatim (a link target, not a built id). */
export function exchangeAnchor(messageSeq: Leaf): string {
  return `exchange-${messageSeq.text}`;
}

export function Exchange({
  message,
  decisions,
  live,
  decisionsPanel,
  sessionId,
  afterSeq,
  permissions = [],
  permissionsPanel,
  onAnswered,
  arriving = [],
  taskId,
  taskCwd,
  taskEntry,
  taskQueued,
  pausedUncertain = false,
}: Props) {
  const text = message.at(".text");
  const textReason = message.at(".text_reason");
  const outcome = message.at(".outcome");
  // `.reply` is `null` or an object: an object leaves no leaf at `.reply` itself (only at its own
  // fields), so its presence is read off one of those fields rather than `.at(".reply")`, which
  // would throw for the common, non-null case.
  const replyOutputKind = leafIfSent(message, ".reply.output_kind");
  const replyText = leafIfSent(message, ".reply.text");
  const replyOutputId = leafIfSent(message, ".reply.output_id");
  const replyReason = message.at(".reply_reason");

  const routed = decisions.filter((decision) => decision.at(".outcome").text === OUTCOME_ROUTED);
  const uncertain = decisions.find((decision) => decision.at(".outcome").text === OUTCOME_UNCERTAIN);
  const fallback = decisions.find((decision) => decision.at(".outcome").text === OUTCOME_FALLBACK);
  // No route chosen and a reason served: routing was off, said as served (ticket 317).
  const servedReason = leafIfSent(message, ".route_reason");
  const routeReason =
    routed.length === 0 && leafIfSent(message, ".route")?.kind === "null" && servedReason?.kind !== "null"
      ? servedReason
      : undefined;
  const label = authorLabel(message, taskQueued);
  const hopRows = rowsOf(message, (row) => `.hops[${row}].hop`);
  const hops = hopRows.map((row) => rowWire(message, `.hops[${row}]`));
  const refused = hops.find(
    (hop) => leafIfSent(hop, ".result")?.text === HOP_FAILED && leafIfSent(hop, ".reason")?.text === REASON_DIRTY_TREE,
  );
  // Who was refused: the task's named entry, else the route of the last agent hop the router chose.
  const refusedAgent = taskEntry !== undefined && taskEntry.kind !== "null" ? taskEntry : routed.filter((decision) => decision.at(".route").text !== ROUTE_DONE).at(-1)?.at(".route");
  const waiting =
    arriving.length > 0 || permissions.some((permission) => permission.at(".state").text === PENDING);

  return (
    <li
      className={styles.exchange}
      id={exchangeAnchor(message.at(".message_seq"))}
      tabIndex={-1}
      data-testid="exchange"
      data-live={live ? "" : undefined}>
      {label === undefined ? null : (
        <p className={styles.author} data-testid="exchange-author">
          {label}
        </p>
      )}
      <p className={styles.message} data-testid="exchange-message">
        {text.kind === "null" ? (
          <span className={styles.textAbsent}>
            {textReason.kind === "null" ? null : <Value leaf={textReason} />}
          </span>
        ) : (
          <Value leaf={text} />
        )}
      </p>

      {routeReason === undefined ? null : (
        <div className={styles.routeLines} data-testid="route-lines">
          <RouteLine routeReason={routeReason} />
        </div>
      )}

      <ResponseSource
        response={decisionsPanel.response}
        method="GET"
        sourceLabel={decisionsPanel.sourceLabel}
      >
        {routed.length === 0 ? null : (
          <div className={styles.routeLines} data-testid="route-lines">
            {routed.map((decision, index) => (
              <RouteLine key={index} decision={decision} first={index === 0} />
            ))}
          </div>
        )}
        {outcome.text === OUTCOME_UNCERTAIN && uncertain !== undefined ? (
          <UncertainStop
            decision={uncertain}
            ran={replyOutputKind !== undefined}
            resume={pausedUncertain ? sessionId : undefined}
          />
        ) : null}
        {outcome.text === OUTCOME_FALLBACK && fallback !== undefined ? (
          <FallbackNotice cause={fallback.at(".fallback_cause")} />
        ) : null}
      </ResponseSource>

      {permissionsPanel === undefined || permissions.length === 0 ? null : (
        <ResponseSource response={permissionsPanel.response} method="GET" sourceLabel={permissionsPanel.sourceLabel}>
          {permissions.map((permission) => (
            <PermissionCard
              key={permission.at(".permission_id").text}
              permission={permission}
              onAnswered={onAnswered ?? noop}
            />
          ))}
        </ResponseSource>
      )}

      {arriving.map((id) => (
        <p key={id} className={styles.live} data-testid="permission-arriving">
          Asking for permission...
        </p>
      ))}

      {waiting ? null : refused !== undefined ? (
        <DirtyTreeRefusal hop={refused} taskId={taskId} cwd={taskCwd} agent={refusedAgent} />
      ) : live && replyOutputKind === undefined ? (
        <p className={styles.live} data-testid="exchange-live">
          Live
        </p>
      ) : replyOutputKind === undefined && outcome.text === OUTCOME_UNCERTAIN ? null : replyOutputKind === undefined ? (
        replyReason.kind === "null" ? (
          <AbsentState error={NO_REPLY_ERROR} message={NO_REPLY_MESSAGE_FALLBACK} />
        ) : (
          <ReplyAbsent reason={replyReason} />
        )
      ) : (
        <>
          <OutputView
            kind={replyOutputKind}
            output={replyText === undefined ? null : outputTextOrNull(replyText)}
            outputId={replyOutputId === undefined ? undefined : definedLeaf(replyOutputId)}
          />
          <ReplyCaption message={message} />
        </>
      )}

      {hops.map((hop, index) => (hop === refused ? null : <RepoRecord key={index} hop={hop} cwd={taskCwd} />))}

      <Steps message={message} live={live} sessionId={sessionId} afterSeq={afterSeq} />
    </li>
  );
}

/** The collapsed "Steps" disclosure: served counts and outputs for a finished exchange, the live
 * trace for the one still running (`ux/experience.md`'s "open while live, closes when it finishes"). */
function Steps({
  message,
  live,
  sessionId,
  afterSeq,
}: {
  message: Wire;
  live: boolean;
  sessionId: string;
  afterSeq: Leaf | undefined;
}) {
  const [open, setOpen] = useState(live);
  // React's own "adjust state when a prop changes" pattern: a transition of `live` resets `open`
  // to match it, during render rather than in an effect, so the disclosure never paints one frame
  // in the stale state first. A manual toggle in between two transitions is left alone.
  const [wasLive, setWasLive] = useState(live);
  if (live !== wasLive) {
    setWasLive(live);
    setOpen(live);
  }
  const bodyId = useId();

  return (
    <div className={styles.steps}>
      <button
        type="button"
        className={styles.stepsToggle}
        aria-expanded={open}
        aria-controls={bodyId}
        onClick={() => setOpen(!open)}
      >
        Steps{live ? <span className={styles.liveTag}>Live</span> : null}
      </button>
      {open ? (
        <div id={bodyId} data-testid="exchange-steps">
          {live ? <TraceTimeline sessionId={sessionId} live={{ afterSeq }} /> : <StepsSummary message={message} />}
        </div>
      ) : null}
    </div>
  );
}

/** The message's own served `steps` count and each hop's outputs - scoped by the store, never
 * inferred from the events stream (`_steps_for`/`_hops_for` are `WHERE message_seq = ?` queries). */
function StepsSummary({ message }: { message: Wire }) {
  const steps = message.at(".steps");
  const hopRows = rowsOf(message, (row) => `.hops[${row}].hop`);

  return (
    <div data-testid="steps-summary">
      <p className={styles.stepsCount}>
        <Value leaf={steps} />
        {" steps"}
      </p>
      {hopRows.map((hopRow) => (
        <HopOutputs key={hopRow} message={message} hopRow={hopRow} />
      ))}
    </div>
  );
}

function HopOutputs({ message, hopRow }: { message: Wire; hopRow: number }) {
  const outputRows = rowsOf(message, (row) => `.hops[${hopRow}].outputs[${row}].output_id`);
  if (outputRows.length === 0) return null;
  return (
    <ul className={styles.hopOutputs}>
      {outputRows.map((outputRow) => {
        const prefix = `.hops[${hopRow}].outputs[${outputRow}]`;
        return (
          <li key={outputRow}>
            <OutputView
              kind={message.at(`${prefix}.output_kind`)}
              output={null}
              outputId={message.at(`${prefix}.output_id`)}
              name={definedLeaf(message.at(`${prefix}.name`))}
              size={message.at(`${prefix}.size_bytes`)}
            />
          </li>
        );
      })}
    </ul>
  );
}
