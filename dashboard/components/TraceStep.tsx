"use client";

/**
 * One event, as the plain sentence `lib/trace-steps.ts` gives it (FR-129, decision 68).
 *
 * The sentence's figures are the frame's own payload fields, each a `Value` (`D-1`, MUST rule 21):
 * this component reads no number and computes nothing, it only walks the sentence parts
 * `lib/trace-steps.ts` returns. A name the table does not know - and every name in
 * `lib/trace-steps.ts::VERBATIM` - renders its own characters in mono instead (AC3), which is
 * also what a name the *taxonomy* does not know would do, since it can never reach a row either.
 *
 * `data-seq` on the root `<li>` is never rendered as text - the same non-drawn hook `Value`'s
 * `data-route`/`data-path` already are - so a walk can still tell rows apart without the seq
 * being drawn (FR-129 AC2 is about what is *shown*, not about every DOM attribute).
 *
 * `ts` is the replay's own leaf for this row; a row that arrived live carries none, because
 * `gateway/sse.py::frame` sends `id`, `event` and `data` only - the same rule the deleted row
 * component this one replaces already kept.
 * Expanded, a step lists every field its payload holds, each a `Value` of the same frame - a flat
 * field list rather than a nested `RecordView`, because every payload this taxonomy publishes is
 * flat or nearly so, and the raw JSON text a `RecordView` needs is not available for a replayed
 * row (`ReplayPanel`'s own note on why the events table holds no such text).
 */
import { useState } from "react";

import { type SentencePart, type StepKind, traceStepFor } from "../lib/trace-steps";
import { humanTimestamp } from "../lib/humanize";
import { now } from "../lib/clock";
import type { Leaf } from "../lib/wire";
import type { StreamFrame } from "../lib/stream";

import { StepIcon } from "./StepIcon";
import { Value } from "./Value";
import styles from "./TraceStep.module.css";

type Props = {
  /** The event, as `lib/stream.ts` or `ReplayPanel`'s own adapter built it. */
  frame: StreamFrame;
  /** The replay's own `ts` leaf, where this step came from the replay rather than the stream. */
  ts?: Leaf;
  /** Just arrived, so the fade runs once. */
  arrived?: boolean;
};

/**
 * The operator's sentence for a name `lib/trace-steps.ts` leaves verbatim but a run now publishes
 * (ticket 232): `memory.write.committed`. That table is outside this ticket's scope, so the
 * sentence lives here, and no screen draws the raw name (`V6-1`, G-57).
 */
const PUBLISHED_VERBATIM: Readonly<Record<string, string>> = {
  "memory.write.committed": "Saved the run to memory",
};

export function TraceStep({ frame, ts, arrived }: Props) {
  const [expanded, setExpanded] = useState(false);
  const row = traceStepFor(frame.name.text);
  const kind = row?.kind(frame.payload) ?? undefined;
  const status = row?.status(frame.payload);
  // Colour marks status only (Direction rule 1): failed and running are the two a step can be in
  // today, and a `fell_back` kind (a guardrail, a failed-open gate, a truncated budget) carries
  // the same warn colour a `TraceStep` fed a live `loop.guardrail.tripped` would want, whether or
  // not `status` itself says so.
  const iconStatus =
    status === "failed" ? "failed" : status === "running" ? "running" : kind === "fell_back" ? "warn" : undefined;

  return (
    <li
      className={styles.step}
      data-testid="trace-step"
      data-seq={frame.seq.text}
      data-arrived={arrived === true ? "" : undefined}
    >
      <span className={styles.row}>
        <span
          className={styles.icon}
          data-testid="step-icon-frame"
          data-status={iconStatus}
        >
          {kind === undefined ? null : <StepIcon kind={kind} />}
        </span>
        <span className={styles.sentence} data-testid="trace-sentence">
          {row === undefined ? (
            <span className={styles.verbatim} data-testid="trace-verbatim">
              {Object.hasOwn(PUBLISHED_VERBATIM, frame.name.text) ? PUBLISHED_VERBATIM[frame.name.text] : frame.name.text}
            </span>
          ) : (
            <Sentence parts={row.sentence(frame.payload)} payload={frame.payload} />
          )}
          {status === "failed" ? <span className={styles.failedWord}>failed</span> : null}
          {status === "running" ? <span className={styles.waitingWord}>waiting</span> : null}
        </span>
        <span className={styles.time} data-testid="trace-step-time">
          {ts === undefined ? null : humanTimestamp(ts, now()).text}
        </span>
        <button
          type="button"
          className={styles.toggle}
          aria-expanded={expanded}
          onClick={() => setExpanded(!expanded)}
        >
          {expanded ? "Collapse" : "Expand"}
        </button>
      </span>
      {expanded ? <Fields frame={frame} /> : null}
    </li>
  );
}

function Sentence({ parts, payload }: { parts: readonly SentencePart[]; payload: StreamFrame["payload"] }) {
  return (
    <>
      {parts.map((part, index) =>
        "text" in part ? (
          <span key={index}>{part.text}</span>
        ) : (
          <Value key={index} leaf={payload.at(part.figure)} />
        ),
      )}
    </>
  );
}

/** Every field the payload holds, each a `Value` of this frame - the expanded detail view. */
function Fields({ frame }: { frame: StreamFrame }) {
  if (frame.paths.length === 0) {
    return <p className={styles.empty}>No payload fields.</p>;
  }
  return (
    <dl className={styles.fields} data-testid="trace-step-fields">
      {frame.paths.map((path) => (
        <FieldRow key={path} path={path} frame={frame} />
      ))}
    </dl>
  );
}

function FieldRow({ path, frame }: { path: string; frame: StreamFrame }) {
  return (
    <>
      <dt className={styles.fieldKey}>{path}</dt>
      <dd className={styles.fieldValue}>
        <Value leaf={frame.payload.at(path)} />
      </dd>
    </>
  );
}

export type { StepKind };
