"use client";

/**
 * One work suite on the Evals page (FR-139, `V6-3`, `V6-8`; design.md's `WorkSuiteRow`).
 *
 * The suite name, "N cases, M awaiting a verdict" and the latest score, every figure a `Leaf` of
 * `/v1/eval/suites` and **no arithmetic**: the counts are served strings, and the only thing
 * decided here is whether the served pending count is the character `0`. "Awaiting a verdict" is a
 * count, not a status, so it stays gray. A suite with verdicts outstanding offers the command that
 * lists them, spelled with the served suite name; every suite also offers the command that runs it, with a verdict in or not.
 * The latest score is the link to its eval run where the gateway served one, and the unknown
 * treatment with its reason ("never run") where it did not.
 */
import { CliCommand } from "../app/evals/CliCommand";
import { evalRunHref } from "../app/evals/limit";
import { nullReasonFor } from "../lib/null-reasons";
import type { Leaf, Wire } from "../lib/wire";

import { Value } from "./Value";
import styles from "./WorkSuiteRow.module.css";

/** The served spelling of a count of none; this tree compares characters, never parsed numbers. */
const NONE_PENDING = "0";
const RUN_NOTE = "to run this suite again:";
const PENDING_NOTE = "to see the cases that wait for your verdict:";

type Props = {
  wire: Wire;
  /** The suite's index in `.suites`. */
  row: number;
};

/** The run command for a suite, spelled with the served name (never a `<placeholder>`). */
function runCommand(suite: Leaf): string {
  return `harnessed eval run ${suite.text}`;
}

/** The pending command for a suite, spelled with the served name (never a `<placeholder>`). */
function pendingCommand(suite: Leaf): string {
  return `harnessed eval verdict ${suite.text} --pending`;
}

function runHref(run: Leaf): string {
  return evalRunHref(run.text);
}

export function WorkSuiteRow({ wire, row }: Props) {
  const suite = wire.at(`.suites[${row}].suite`);
  const total = wire.at(`.suites[${row}].cases_total`);
  const pending = wire.at(`.suites[${row}].cases_pending`);
  const run = wire.at(`.suites[${row}].latest_eval_run_id`);
  const score = wire.at(`.suites[${row}].latest_score`);
  const awaiting = pending.text !== NONE_PENDING;

  return (
    <li className={styles.row} aria-label={suite.text} data-testid="work-suite-row">
      <div className={styles.head}>
        <span className={styles.name}>
          <Value leaf={suite} />
        </span>
        <span className={styles.kind}>Work suite, recorded from your sessions</span>
      </div>
      <p className={styles.cases}>
        <Value leaf={total} /> cases,{" "}
        {awaiting ? (
          <>
            <Value leaf={pending} /> awaiting a verdict
          </>
        ) : (
          "all with a verdict"
        )}
      </p>
      <p className={styles.latest}>
        <span className={styles.latestLabel}>Latest score</span>{" "}
        {run.kind === "null" || score.kind === "null" ? (
          <Value leaf={score} nullReason={nullReasonFor(score.route, score.path)} />
        ) : (
          <a
            className={styles.score}
            href={runHref(run)}
            data-figure=""
            data-route={score.route}
            data-path={score.path}
          >
            {score.text}
          </a>
        )}
      </p>
      <CliCommand note={RUN_NOTE} command={runCommand(suite)} />
      {awaiting ? <CliCommand note={PENDING_NOTE} command={pendingCommand(suite)} /> : null}
    </li>
  );
}
