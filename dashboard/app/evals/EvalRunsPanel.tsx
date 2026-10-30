"use client";

/**
 * `/evals`, the listing: every eval run the ledger holds, in the gateway's order (FR-56, UJ-9).
 *
 * One panel, one request, one table. The rows are the response's rows and their order is the
 * response's order - there is no sort control to offer and nothing here reorders anything (`D-1`,
 * MUST rule 21). Each row's link is its own `eval_run_id`, taken verbatim from the row that
 * carries it, and so are the two ids a comparison is opened at (MUST rule 25).
 *
 * **A run with no cases reads `unknown`, never `0.0`.** `runner.recent` folds zero evidence to
 * `0.0` so the promoter fails closed; FR-47 makes the emitters send `null` instead, because on a
 * screen a `0.0` is a measurement and this is the absence of one. The sentence beside it is
 * `lib/null-reasons.ts`'s, keyed on this route and `.runs[].score`, and it is only ever shown
 * where the leaf really is a null - asking for a reason beside a measured score would print
 * "no evidence: 0 cases" next to the evidence, which is ticket 104's trap in a new place.
 *
 * The `?limit=` control and the compare picker are the two things this page owns, and both end in
 * a URL: choosing 50 is a navigation, and picking a pair is a link to `/evals?before=&after=` carrying
 * both ids. Nothing is remembered between two pages (`D-6`).
 */
import { useState } from "react";

import { useRouter } from "next/navigation";

import { DataTable } from "../../components/DataTable";
import { PanelFrame } from "../../components/PanelFrame";
import { Value } from "../../components/Value";
import { now } from "../../lib/clock";
import { humanAgo } from "../../lib/humanize";
import { nullReasonFor } from "../../lib/null-reasons";
import type { Leaf, Wire } from "../../lib/wire";

import { rowsOf } from "../../lib/rows";
import { compareHref, evalRunHref, footerLine, listingHref, listingRoute, LIMITS } from "./limit";
import styles from "./evals.module.css";

/** What an operator runs to put a row here, where the eval ledger is empty (`EmptyState`, `D-3`). */
const CLI_TWIN = "harnessed eval run";

/** The path the score's documented absence is keyed on, written for every row at once. */
const SCORE_PATH = ".runs[].score";

/** What the page says while only one side of a comparison has been picked, or neither. */
const COMPARE_PROMPT = "pick a before and an after to compare two eval runs";

type Props = {
  /** The limit the URL is carrying, as its characters. */
  limit: string;
};

/** The pair being compared: two eval run ids, each exactly as a response field spelled it. */
type Pair = { before?: string; after?: string };

export function EvalRunsPanel({ limit }: Props) {
  const route = listingRoute(limit);
  const [pair, setPair] = useState<Pair>({});

  return (
    <PanelFrame route={route} title="Eval runs" sourceLabel="Ledger: all time" cliTwin={CLI_TWIN}>
      {(wire) => {
        const rows = rowsOf(wire, (index) => `.runs[${index}].eval_run_id`);
        if (rows.length === 0) return null;
        const idOf = (row: number): string => wire.at(`.runs[${row}].eval_run_id`).text;
        return (
          <div className={styles.body}>
            <div className={styles.controls}>
              <LimitControl limit={limit} />
              <ComparePicker pair={pair} />
            </div>
            <DataTable
              caption="Eval runs, in the order the gateway sent them"
              rows={rows}
              rowLink={{
                header: "eval run id",
                leaf: (row) => wire.at(`.runs[${row}].eval_run_id`),
                href: (row) => evalRunHref(idOf(row)),
              }}
              columns={[
                { header: "suite", cell: (row) => <Value leaf={wire.at(`.runs[${row}].suite`)} /> },
                { header: "label", cell: (row) => <Value leaf={wire.at(`.runs[${row}].label`)} /> },
                {
                  header: "started",
                  cell: (row) => {
                    const started = wire.at(`.runs[${row}].started_at`);
                    return (
                      <span className={styles.nowrap}>
                        <Value
                          leaf={started}
                          human={started.kind === "null" ? undefined : humanAgo(started, now())}
                        />
                      </span>
                    );
                  },
                },
                {
                  header: "cases",
                  cell: (row) => <Value leaf={wire.at(`.runs[${row}].case_count`)} />,
                },
                {
                  header: "passes",
                  cell: (row) => <Value leaf={wire.at(`.runs[${row}].passes`)} />,
                },
                { header: "score", cell: (row) => scoreCell(wire, route, row) },
                {
                  header: "samples",
                  cell: (row) => <Value leaf={wire.at(`.runs[${row}].samples`)} />,
                },
                { header: "provider", cell: (row) => sourceCell(wire, route, row, "provider") },
                { header: "model", cell: (row) => sourceCell(wire, route, row, "model") },
                {
                  header: "before",
                  cell: (row) => (
                    <SidePicker side="before" id={idOf(row)} pair={pair} pick={setPair} />
                  ),
                },
                {
                  header: "after",
                  cell: (row) => (
                    <SidePicker side="after" id={idOf(row)} pair={pair} pick={setPair} />
                  ),
                },
              ]}
            />
            <p className={styles.footer} data-testid="evals-footer">
              {footerLine(limit)}
            </p>
          </div>
        );
      }}
    </PanelFrame>
  );
}

/**
 * One row's score, with the reason the ledger sent nothing where it sent nothing.
 *
 * The reason is looked up only after the leaf says it is a null. `nullReasonFor` is keyed on a
 * route and a path alone, so handing it a measured score would print the zero-evidence sentence
 * beside real evidence - the same trap ticket 104 recorded for a decided improvement cycle.
 */
function scoreCell(wire: Wire, route: string, row: number) {
  const leaf = wire.at(`.runs[${row}].score`);
  return <Value leaf={leaf} nullReason={reasonIf(leaf, route, SCORE_PATH)} />;
}

/**
 * One row's `provider` or `model`, as served (FR-72). A run opened before migration 010 sends
 * `null`, which reads `unknown` with the sentence keyed on this route and path, never a blank.
 */
function sourceCell(wire: Wire, route: string, row: number, field: "provider" | "model") {
  const leaf = wire.at(`.runs[${row}].${field}`);
  return <Value leaf={leaf} nullReason={reasonIf(leaf, route, `.runs[].${field}`)} />;
}

/** The documented sentence for this absence, or nothing at all where there is no absence. */
export function reasonIf(leaf: Leaf, route: string, path: string): string | undefined {
  return leaf.kind === "null" ? nullReasonFor(route, path) : undefined;
}

/**
 * The 20 / 50 / 100 choice, as a radio group, because it is one choice out of a closed set and a
 * radio says so to a keyboard and a screen reader without anything being scripted. Choosing
 * pushes the URL; the URL is read back by the page, which is what makes the choice survive a
 * reload and a shared link without a single remembered byte.
 */
function LimitControl({ limit }: Props) {
  const router = useRouter();

  return (
    <fieldset className={styles.limit} data-testid="limit-control">
      <legend className={styles.limitLegend}>rows to ask for</legend>
      {LIMITS.map((choice) => (
        <label key={choice} className={styles.limitChoice}>
          <input
            type="radio"
            name="limit"
            value={choice}
            checked={choice === limit}
            onChange={() => router.push(listingHref(choice))}
          />
          {choice}
        </label>
      ))}
    </fieldset>
  );
}

/**
 * One row's radio for one side of the comparison. Its accessible name is the side and the id, so
 * "before: eval_..." is what a screen reader hears and what a keyboard lands on, rather than a
 * bare radio in a cell with nothing to tell it from the one beside it.
 *
 * Picking changes nothing about the table: the rows stay in the gateway's order and no row is
 * hidden (`D-1`). The only thing a pick moves is the link at the top of the panel.
 */
function SidePicker({
  side,
  id,
  pair,
  pick,
}: {
  side: "before" | "after";
  id: string;
  pair: Pair;
  pick: (next: Pair) => void;
}) {
  return (
    <input
      type="radio"
      name={side}
      value={id}
      aria-label={`${side}: ${id}`}
      checked={pair[side] === id}
      onChange={() => pick({ ...pair, [side]: id })}
    />
  );
}

/**
 * Where the picked pair goes. There is no link until both sides are picked, because a comparison
 * of one run is not a comparison and a half-built URL is a page the gateway would refuse; until
 * then the panel says what it is still waiting for.
 */
function ComparePicker({ pair }: { pair: Pair }) {
  const { before, after } = pair;

  return (
    <p className={styles.compare}>
      {before === undefined || after === undefined ? (
        <span data-testid="compare-prompt">{COMPARE_PROMPT}</span>
      ) : (
        <a className={styles.link} data-testid="compare-link" href={compareHref(before, after)}>
          {`Compare ${before} with ${after}`}
        </a>
      )}
    </p>
  );
}
