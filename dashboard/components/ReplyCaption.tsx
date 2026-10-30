"use client";

/**
 * The line under a reply: who handled it, how many steps, how long, and what it cost (FR-122,
 * UJ-16, UJ-18, UJ-19; design.md "The console in v5"). Every figure is a served `Value`; the one
 * derived-looking word is `humanSeconds`, which lives in the one allowlisted file that may read a
 * number (`lib/humanize.ts`, decision 32).
 *
 * The handler figure is the message's own `handlers` row, never the generic `handler` field
 * (`"groq"|"agent"|"human"`, decision 61's bucket): a session's `handlers` list (decision 64)
 * carries the actual provider or registry entry that acted, which is what "Groq" or "Claude Code"
 * names. The router's own row (`kind: "router"`) is never the reply's handler and is excluded.
 *
 * The cost figure is one exchange's own `CostLine` (ticket 189): `handlers` scoped to this
 * message's own reply handler, the one row `replyHandlerRows` picks below. `CostLine` reads that
 * row's `billing`, `cost`, `api_equivalent` and the store's own `reason` for it - never a second,
 * client-guessed explanation.
 */
import { rowsOf } from "../lib/rows";
import { humanWord } from "../lib/human-words";
import { humanSeconds } from "../lib/humanize";
import type { Leaf, Wire } from "../lib/wire";

import { CostLine } from "./CostLine";
import { SourceChip } from "./SourceChip";
import { Value } from "./Value";
import styles from "./ReplyCaption.module.css";

/** `loop/runs.py::HANDLER_ROUTER` - Jev's own bucket, never the reply's own handler. */
const HANDLER_KIND_ROUTER = "router";

/** The caption's own chip: design.md's "... - Details", opening the message's provenance. */
const DETAILS = "Details";

type Props = {
  /** One row of `/v1/sessions/{id}/messages`, scoped to this message (decision 42). */
  message: Wire;
};

function handlerHuman(handler: Leaf): { text: string; exact: string } | undefined {
  const word = humanWord("handler", handler.text);
  return word === undefined ? undefined : { text: word, exact: handler.text };
}

/** `handlers`, scoped to one row: `CostLine`'s `[0]` addresses it, and any other index throws so
 * `rowsOf` stops after it (decision 42: a `Wire` subtree travels, `Conversation.tsx::rowWire`'s
 * own precedent narrowed to a single row). */
function oneRowWire(wire: Wire, prefix: string): Wire {
  return {
    route: wire.route,
    text: wire.text,
    byteLength: wire.byteLength,
    at: (path: string) => {
      if (!path.startsWith("[0]")) throw new Error(`no leaf at ${path}`);
      return wire.at(`${prefix}${path.slice("[0]".length)}`);
    },
  };
}

/** Every `.handlers[]` row that is not the router's own bucket, in the served order. */
function replyHandlerRows(message: Wire): number[] {
  return rowsOf(message, (row) => `.handlers[${row}].handler`).filter(
    (row) => message.at(`.handlers[${row}].kind`).text !== HANDLER_KIND_ROUTER,
  );
}

/** The agent entry a Groq synthesis pass wrote the reply up from: the reply's handler is the chat
 * model and a `kind: "agent"` row sits beside it (ticket 198's closing pass). */
function synthesisSource(message: Wire): number | undefined {
  const all = rowsOf(message, (row) => `.handlers[${row}].handler`);
  const kindOf = (row: number) => message.at(`.handlers[${row}].kind`).text;
  const last = replyHandlerRows(message).at(-1);
  if (last === undefined || kindOf(last) !== "chat_model") return undefined;
  return all.find((row) => kindOf(row) === "agent");
}

export function ReplyCaption({ message }: Props) {
  const rows = replyHandlerRows(message);
  const row = rows.at(-1);
  const writtenFrom = synthesisSource(message);
  const steps = message.at(".steps");
  const seconds = message.at(".seconds");

  return (
    <p className={styles.caption} data-testid="reply-caption">
      {row === undefined ? null : (
        <span className={styles.segment} data-testid="caption-segment">
          <HandlerFigure message={message} row={row} />
        </span>
      )}
      {writtenFrom === undefined ? null : (
        <span className={styles.segment} data-testid="caption-segment">
          {"Written up by Groq from "}
          <Value leaf={message.at(`.handlers[${writtenFrom}].handler`)} />
          {"'s result"}
        </span>
      )}
      <span className={styles.segment} data-testid="caption-segment">
        <Value leaf={steps} />
        {" steps"}
      </span>
      {seconds.kind === "null" ? null : (
        <span className={styles.segment} data-testid="caption-segment">
          <Value leaf={seconds} human={humanSeconds(seconds)} />
        </span>
      )}
      {row === undefined ? null : (
        <span className={styles.segment} data-testid="caption-segment">
          <CostLine handlers={oneRowWire(message, `.handlers[${row}]`)} />
        </span>
      )}
      <SourceChip label={DETAILS} leaf={message.at(".message_seq")} />
    </p>
  );
}

function HandlerFigure({ message, row }: { message: Wire; row: number }) {
  const handler = message.at(`.handlers[${row}].handler`);
  return <Value leaf={handler} human={handlerHuman(handler)} />;
}
