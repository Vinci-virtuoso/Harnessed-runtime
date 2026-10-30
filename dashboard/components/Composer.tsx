"use client";

/**
 * The console's one input, split out of `ConsolePanel` so the conversation and the box that sends
 * to it are two components (FR-122; design.md "The console in v5"). It still posts exactly one
 * string under the key `prompt`, either to open a run or to continue the one `session` names (MUST
 * rule 26): the runtime carries the session's messages, and the browser never assembles a history.
 *
 * A send the control app refuses is quoted verbatim beneath the input, in the same strip the
 * typed text stays in (MUST rule 23); a send it accepts clears the text and hands the `202` to
 * `onSent`, which is the only way this component talks to its caller.
 */
import { type FormEvent, type KeyboardEvent, useId, useState } from "react";

import { type ControlResult, queueTask, sendMessage, startRun } from "../lib/control-client";
import type { Leaf, Wire } from "../lib/wire";

import { ErrorState } from "./ErrorState";
import { QueuedNotice } from "./QueuedNotice";
import { UnreachableState } from "./UnreachableState";
import styles from "./Composer.module.css";

/** design.md's `Composer`, in its own words. */
export const COMPOSER_LABEL = "Message to the harness";
export const PLACEHOLDER = "Message Harnessed";
export const SEND = "Send";
export const SENDING = "Sending…";
export const IN_FLIGHT_REASON = "Harnessed is working";
export const READING = "Reading this session";
export const QUEUE = "Queue as a task";
export const TASK_RUNNING_REASON = "Harnessed is working on this task";
export const TASK_PERMISSION_REASON = "Claude Code is waiting for your answer above";
export const PLACEHOLDER_UNCERTAIN = "Reply to continue the task";
export const TASK_UNCERTAIN_REASON = "Your reply resumes the task";

/** What the task's own read says about whether the composer may be used (FR-154, G-45). */
export type TaskState = { state: Leaf; pause: Leaf };

/**
 * The composer's words for a task that is running or paused, and whether they close the input.
 * `undefined` for every other state: the session's own rules apply.
 */
export function taskNote(task: TaskState | undefined): { reason: string; closes: boolean } | undefined {
  if (task === undefined) return undefined;
  if (task.state.text === "running") return { reason: TASK_RUNNING_REASON, closes: true };
  if (task.state.text !== "needs_human") return undefined;
  if (task.pause.text === "permission") return { reason: TASK_PERMISSION_REASON, closes: true };
  if (task.pause.text === "uncertain") return { reason: TASK_UNCERTAIN_REASON, closes: false };
  return undefined;
}

/** Who answers a refusal under the composer: the control app, never the read gateway. */
const CONTROL_APP = "control app";

/** The `202` a send opened: the only shape `onSent` is ever called with. */
export type Opened = Extract<ControlResult, { state: "ok" }>;

/** A send that did not open a run: the control app's refusal, or no answer at all. */
type Refused = Exclude<ControlResult, { state: "ok" }>;

type Props = {
  /** A session to continue, or `null` to open a new one (MUST rule 25). */
  session: string | null;
  /** Whether a run is already in flight on this session: disables the input and the button. */
  inFlight: boolean;
  /** Whether the session's conversation has not landed yet: disables the composer like `inFlight`,
   * which takes precedence (ticket 216). */
  reading?: boolean;
  /** The task this session belongs to, as its own read last said (FR-154). */
  task?: TaskState;
  /** Called once a send is accepted, with the `202` whole. */
  onSent: (opened: Opened) => void;
};

export function Composer({ session, inFlight, reading = false, task, onSent }: Props) {
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const [refused, setRefused] = useState<Refused | undefined>(undefined);
  const [queued, setQueued] = useState<{ text: string; wire: Wire } | undefined>(undefined);
  const reasonId = useId();
  const note = taskNote(task);
  const blocked = inFlight || reading || note?.closes === true;
  const reason = note !== undefined ? note.reason : inFlight ? IN_FLIGHT_REASON : READING;
  // A paused task's reply hint is shown while the input is open; a closed input always says why.
  const placeholder = task?.state.text === "needs_human" && task.pause.text === "uncertain" ? PLACEHOLDER_UNCERTAIN : PLACEHOLDER;
  const hinted = blocked || note !== undefined;
  const queuedString = queued?.text;

  async function send(): Promise<void> {
    if (text === "" || sending || blocked) return;
    setSending(true);
    setRefused(undefined);
    // One string either way: a new run, or one more message of the open one (MUST rule 26).
    const result = session === null ? await startRun(text) : await sendMessage(session, text);
    setSending(false);
    if (result.state !== "ok") {
      setRefused(result);
      return;
    }
    setText("");
    onSent(result);
  }

  /** The one string, queued as a task instead of sent: the console never starts it (MUST rule 26). */
  async function queue(): Promise<void> {
    if (text === "" || sending || blocked) return;
    setSending(true);
    setRefused(undefined);
    setQueued(undefined);
    const result = await queueTask(text);
    setSending(false);
    if (result.state !== "ok") {
      setRefused(result);
      return;
    }
    setQueued({ text, wire: result.wire });
    setText("");
  }

  function onSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    void send();
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>): void {
    // Enter sends; Shift+Enter is the textarea's own newline.
    if (event.key !== "Enter" || event.shiftKey) return;
    event.preventDefault();
    void send();
  }

  return (
    <form className={styles.composer} onSubmit={onSubmit}>
      <label className={styles.label} htmlFor={`${reasonId}-input`}>
        {COMPOSER_LABEL}
      </label>
      <div className={styles.row}>
        <textarea
          id={`${reasonId}-input`}
          className={styles.input}
          rows={1}
          value={text}
          placeholder={placeholder}
          disabled={blocked}
          aria-describedby={hinted ? reasonId : undefined}
          onChange={(event) => setText(event.target.value)}
          onKeyDown={onKeyDown}
        />
        <button type="submit" className={styles.send} disabled={text === "" || sending || blocked}>
          {sending ? SENDING : SEND}
        </button>
        <button type="button" className={styles.queue} disabled={text === "" || sending || blocked} onClick={() => void queue()}>
          {QUEUE}
        </button>
      </div>
      {hinted ? (
        <p id={reasonId} className={styles.reason} role="status">
          {reason}
        </p>
      ) : null}
      {refused === undefined ? null : <Refusal refused={refused} />}
      {queued === undefined || queuedString === undefined ? null : <QueuedNotice text={queuedString} wire={queued.wire} />}
    </form>
  );
}

/** A send the control app refused, quoted; or one it never answered (MUST rule 23). */
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
