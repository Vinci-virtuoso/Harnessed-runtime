"use client";

/**
 * One loop run, read back field by field (FR-55, UJ-7).
 *
 * Every figure is a leaf of this panel's own response and carries the path it was read from, so
 * `input_tokens` is one Tab from `/runs/{run_key} -> .usage.input_tokens` (`D-4`). Nothing is
 * summed: the three token counts are the three the gateway sent, and there is no total, because a
 * total would be a fourth number no ledger holds.
 *
 * **`usage: null` is one `unknown`, not three zeroes.** A run written before migration 006 has no
 * usage at all, and `design.md` gives that absence its own sentence: a partial sum would
 * understate the run. So the panel says the one thing that is true - usage is unknown, and why -
 * rather than inventing the three counts the response does not contain.
 *
 * **A `case_run` is named and not linked** (`[GAP G-4]`). The key of an eval case holds the eval
 * run id and the case id, and the gateway sends neither as a field. Splitting the key on `:` to
 * build a link is the client deriving an id, which MUST rule 25 refuses by name, so the panel says
 * what the row is and stops there. A `session` run does link on: its `run_key` *is* the session id
 * on the `harnessed run` path, so the link is that field verbatim and nothing is assembled.
 */
import { KeyKindTag } from "../../components/KeyKindTag";
import { PanelFrame } from "../../components/PanelFrame";
import { StatTile } from "../../components/StatTile";
import { StopReasonTag } from "../../components/StopReasonTag";
import { Value } from "../../components/Value";
import { nullReasonFor } from "../../lib/null-reasons";
import type { Wire } from "../../lib/wire";

import { sessionHref } from "./limit";
import { leafIfSent } from "../../lib/rows";
import styles from "./runs.module.css";

/** The kinds `key_kind` is spelled with, compared as characters as everything here is. */
const CASE_RUN = "case_run";
const SESSION = "session";

/** What a case run is, said in words because there is no eval run id to link to (`[GAP G-4]`). */
const CASE_RUN_NOTE = [
  "This is one case of an eval run.",
  "The gateway sends no eval run id or case id as a field, so there is no link back to that run:",
  "splitting the key would be this page inventing an id (MUST rule 25).",
].join(" ");

/** The three counts a run records, in the order the payload spells them. */
const USAGE_FIELDS = ["input_tokens", "output_tokens", "cached_input_tokens"];

type Props = {
  /** The key the URL carried, passed to the gateway exactly as it arrived. */
  runKey: string;
};

export function RunPanel({ runKey }: Props) {
  const route = `/v1/runs/${runKey}`;

  return (
    <PanelFrame route={route} title="Run" sourceLabel="Ledger: all time">
      {(wire) => (
        <div className={styles.body}>
          <div className={styles.identity}>
            <Value leaf={wire.at(".run_key")} />
            <KeyKindTag leaf={wire.at(".key_kind")} />
          </div>
          {kindNote(wire)}
          <div className={styles.tiles}>
            <StatTile label="iterations" leaf={wire.at(".iterations")} />
            <StatTile label="tool calls" leaf={wire.at(".tool_calls")} />
            {usageTiles(wire, route)}
          </div>
          <dl className={styles.fields}>
            <dt className={styles.term}>started</dt>
            <dd className={styles.detail}>
              <Value leaf={wire.at(".started_at")} />
            </dd>
            <dt className={styles.term}>finished</dt>
            <dd className={styles.detail}>
              <Value leaf={wire.at(".finished_at")} />
            </dd>
            <dt className={styles.term}>stop reason</dt>
            <dd className={styles.detail}>
              <StopReasonTag
                leaf={wire.at(".stop_reason")}
                nullReason={nullReasonFor(route, ".stop_reason")}
              />
            </dd>
            <dt className={styles.term}>reply</dt>
            <dd className={styles.detail}>
              <Value leaf={wire.at(".reply")} />
            </dd>
          </dl>
        </div>
      )}
    </PanelFrame>
  );
}

/**
 * What this run is, and where it goes. The two kinds are different facts and get different
 * treatments: one is a session with its own event stream, the other is a case with nowhere to go.
 */
function kindNote(wire: Wire) {
  const kind = wire.at(".key_kind");

  if (kind.text === CASE_RUN) {
    return (
      <p className={styles.note} data-testid="run-kind-note">
        {CASE_RUN_NOTE}
      </p>
    );
  }

  if (kind.text !== SESSION) return null;

  const key = wire.at(".run_key");
  return (
    <p className={styles.note}>
      <a className={styles.link} data-testid="session-events-link" href={sessionHref(key.text)}>
        Events for this session
      </a>
    </p>
  );
}

/**
 * The three counts, or the one absence. `lib/wire.ts` gives a leaf only where the body holds a
 * primitive, so a `.usage` leaf existing at all is the response saying `usage` is `null`: an
 * object has no leaf of its own. That is the whole test, and it needs no field to be guessed at.
 */
function usageTiles(wire: Wire, route: string) {
  const absent = leafIfSent(wire, ".usage");
  if (absent !== undefined) {
    return <StatTile label="usage" leaf={absent} nullReason={nullReasonFor(route, ".usage")} />;
  }

  return USAGE_FIELDS.map((field) => (
    <StatTile key={field} label={label(field)} leaf={wire.at(`.usage.${field}`)} />
  ));
}

/** A payload field as a tile's name: the same word, spaced for reading. */
function label(field: string): string {
  return field.replaceAll("_", " ");
}
