"use client";

/**
 * The reply area when Jev stopped rather than guess (FR-122, UJ-22; design.md "The console in
 * v5", decision 59). At hop 0 nothing ran, so there is no reply, only the decision that stopped,
 * in words. At a later hop a step ran and its output is the reply (decision 60): `ran` says which,
 * and the reply renders beside this notice.
 *
 * It reads one line, `StopLine` ("78%, the router's confidence, below its 80% floor"), from the
 * decision's own `confidence_percent`, `compared` and `floor_percent` (never a comparison computed
 * here); a "Needs you" stop has no floor and reads its `decision_stop_reason`. Then the
 * top routes Jev considered, each a stored `probability_percent` - the router's own ranking, never
 * sorted or filtered by the browser (`D-1`, decision 32).
 */
import { leafIfSent, rowsOf } from "../lib/rows";
import { humanWord } from "../lib/human-words";
import type { Leaf, Wire } from "../lib/wire";

import { CopyLine } from "./CopyLine";
import { Value } from "./Value";
import styles from "./UncertainStop.module.css";

const HEADLINE = "Not sure enough to act.";
const NOTHING_RAN = "Nothing ran.";
const HEADLINE_RAN = "Stopped here.";
const FOOTER_RAN = "The last step's output is the reply.";
export const PAUSED_LINE = "The task is paused until you answer.";

/** `router/questions.py`'s route question, among the two this milestone asks per decision. */
const QUESTION_ROUTE = "route";

type Props = {
  /** The uncertain decision, one row of `/v1/sessions/{id}/decisions` (decision 42). */
  decision: Wire;
  /** Whether a step ran before the stop: the served reply is present (read, never derived). */
  ran: boolean;
  /** The session to resume from a terminal, when this is the stop a paused task waits on (FR-154, G-45). */
  resume?: string;
};

function stopReasonHuman(stopReason: Leaf): { text: string; exact: string } | undefined {
  const word = humanWord("decision_stop_reason", stopReason.text);
  return word === undefined ? undefined : { text: word, exact: stopReason.text };
}

function routeHuman(option: Leaf): { text: string; exact: string } | undefined {
  const word = humanWord("route_option", option.text) ?? humanWord("route", option.text);
  return word === undefined ? undefined : { text: word, exact: option.text };
}

/** The `route` question's own options, in the order Jev's answer stored them - never re-sorted. */
function routeOptionRows(decision: Wire): number[] {
  return rowsOf(decision, (row) => `.options[${row}].option`).filter(
    (row) => decision.at(`.options[${row}].question`).text === QUESTION_ROUTE,
  );
}

/**
 * "{figure}, {word}, below its {floor} floor": the figure the stop compared, the word for which
 * figure it was (`compared` through `human-words.ts`), and the floor it fell under, each as served
 * (decision 101). "floor" appears once; nothing is compared or computed here (`D-7`).
 */
export function StopLine({ figure, compared, floor }: { figure: Leaf; compared: Leaf | undefined; floor: Leaf }) {
  const word = compared === undefined || compared.kind === "null" ? undefined : humanWord("compared", compared.text);
  return (
    <>
      <Value leaf={figure} />
      {compared === undefined || word === undefined ? null : (
        <>
          {", "}
          <Value leaf={compared} human={{ text: word, exact: compared.text }} />
        </>
      )}
      {", below its "}
      <Value leaf={floor} />
      {" floor"}
    </>
  );
}

export function UncertainStop({ decision, ran, resume }: Props) {
  const stopReason = decision.at(".stop_reason");
  const floorPercent = decision.at(".floor_percent");
  const confidencePercent = leafIfSent(decision, ".confidence_percent") ?? decision.at(".floor_percent");
  const hasFigure = leafIfSent(decision, ".confidence_percent") !== undefined;
  const routes = routeOptionRows(decision);

  return (
    <div className={styles.stop} role="status" data-testid="uncertain-stop">
      <p className={styles.headline}>{ran ? HEADLINE_RAN : HEADLINE}</p>
      <p className={styles.reason} data-testid="uncertain-reason">
        {floorPercent.kind === "null" || !hasFigure || confidencePercent.kind === "null" ? (
          stopReason.kind === "null" ? null : <Value leaf={stopReason} human={stopReasonHuman(stopReason)} />
        ) : (
          <StopLine figure={confidencePercent} compared={leafIfSent(decision, ".compared")} floor={floorPercent} />
        )}
      </p>
      {routes.length === 0 ? null : (
        <ul className={styles.routes} data-testid="uncertain-routes">
          {routes.map((row) => {
            const option = decision.at(`.options[${row}].option`);
            const probabilityPercent = decision.at(`.options[${row}].probability_percent`);
            return (
              <li key={row} className={styles.route}>
                <Value leaf={option} human={routeHuman(option)} />
                {" "}
                <Value leaf={probabilityPercent} />
              </li>
            );
          })}
        </ul>
      )}
      <p className={styles.footer}>{ran ? FOOTER_RAN : NOTHING_RAN}</p>
      {resume === undefined ? null : (
        <>
          <p className={styles.footer} data-testid="uncertain-paused">
            {PAUSED_LINE}
          </p>
          <CopyLine line={`harnessed run --session ${resume} "<reply>"`} label="Copy" testId="resume-command" />
        </>
      )}
    </div>
  );
}
