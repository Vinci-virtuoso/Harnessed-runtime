"use client";

/**
 * The eval comparison, rendered as the gateway served it (FR-56, FR-62, UJ-9).
 *
 * One panel over one response. FR-62 is `scoring.compare` applied by the gateway to the two eval
 * runs the URL names, so what an operator reads here is the comparison the promoter itself decides
 * on - not a second one assembled in the browser from two run responses (MUST rule 16 at the
 * gateway, MUST rule 21 here). That is why this page reads `/v1/eval/compare` and never
 * `/v1/eval/runs/{eval_run_id}`: two responses merged into one figure would be a second source of
 * truth about a comparison, which is the defect `D-1` exists to prevent.
 *
 * **Nothing is subtracted.** `ScoreDelta` carries a `score_before` and a `score_after` and no
 * difference field, so any difference here would be one the browser invented. A `ScorePair` is
 * therefore the whole rendering of a move, at run level and per case alike: two read figures with
 * a plain `->` between them, each with its own source, and no arrow, colour or glyph carrying a
 * direction (MUST rules 1 and 21).
 *
 * **A case in no list is in no list.** The comparison lists cases that moved, appeared or went
 * missing, and the page says so in a sentence: printing every other case as "unchanged" would be
 * this page claiming a fact about cases the response never mentioned.
 *
 * **A zero-case side is an explained `unknown`.** FR-47 makes the emitters send `score: null` where
 * a side folded to 0 cases, because the promoter's fail-closed `0.0` is a scoring convention and
 * not a measurement. `ScorePair` renders that side as the `unknown` treatment with the sentence
 * `lib/null-reasons.ts` keys on this route and `.before.score` / `.after.score`.
 *
 * **Until both ids are in the URL nothing is asked of the gateway.** A comparison of one eval run
 * is not a comparison, so the page offers an `IdLookup` per side and sends its first request once
 * the URL carries both. An id the URL carries and leaves empty is still characters the URL
 * carried: whether an empty id is servable is the gateway's answer to give, and its own 400
 * `ParameterError` is what an operator then sees.
 */
import { DataTable } from "../../components/DataTable";
import { IdLookup } from "../../components/IdLookup";
import { PanelFrame } from "../../components/PanelFrame";
import { ScorePair } from "../../components/ScorePair";
import { Value } from "../../components/Value";
import type { Wire } from "../../lib/wire";

import { reasonIf } from "./EvalRunsPanel";
import { evalRunHref } from "./limit";
import { rowsOf } from "../../lib/rows";
import { SIDES, type Side, compareRoute, sideHref } from "./query";
import styles from "./compare.module.css";

/** The panel's heading, which is also how a screen reader and a test find it. */
export const PANEL_TITLE = "Eval comparison";

/** What the page says where a list the comparison serves holds nothing at all (`D-3`). */
export const NO_MOVEMENT = "no case moved between these eval runs";

/** The sentence that keeps a silent case from being read as a claim (PRD glossary, FR-56). */
export const ONLY_MOVED =
  "this comparison lists only the cases that moved, are new or are missing: a case that did not move is in no list";

/** What the page says while the URL names fewer than two eval run ids. */
export const NEEDS_BOTH = "a comparison needs a before and an after eval run id";

/** The two lists whose rows are a `ScoreDelta`, in the order FR-62 serves them. */
const MOVED: readonly { field: string; title: string }[] = [
  { field: "regressions", title: "Regressions" },
  { field: "soft_drops", title: "Soft drops" },
];

/** The two lists whose rows are a bare case id, in the order FR-62 serves them. */
const NAMED: readonly { field: string; title: string }[] = [
  { field: "new_cases", title: "New cases" },
  { field: "missing_cases", title: "Missing cases" },
];

type Props = {
  /** The before side's eval run id, as the URL carried it, or nothing where it carried none. */
  before?: string;
  /** The after side's eval run id, as the URL carried it, or nothing where it carried none. */
  after?: string;
};

export function ComparePanel({ before, after }: Props) {
  if (before === undefined || after === undefined) {
    return <ComparePicker before={before} after={after} />;
  }
  return <Comparison route={compareRoute(before, after)} />;
}

/** One comparison, read from the one route that serves one. */
function Comparison({ route }: { route: string }) {
  return (
    <PanelFrame route={route} title={PANEL_TITLE} sourceLabel="Ledger: all time">
      {(wire) => (
        <div className={styles.body}>
          <p className={styles.headline}>
            <span className={styles.headlineLabel}>score</span>
            <ScorePair before={wire.at(".before.score")} after={wire.at(".after.score")} />
          </p>
          <Sides wire={wire} />
          <p className={styles.note} data-testid="compare-note">
            {ONLY_MOVED}
          </p>
          {MOVED.map((list) => (
            <MovedCases key={list.field} wire={wire} field={list.field} title={list.title} />
          ))}
          {NAMED.map((list) => (
            <NamedCases key={list.field} wire={wire} field={list.field} title={list.title} />
          ))}
        </div>
      )}
    </PanelFrame>
  );
}

/**
 * The two sides, one row each, in the order a comparison reads: before, then after.
 *
 * Each row's link is that side's own `eval_run_id` field, taken verbatim from the comparison that
 * named it (MUST rule 25), so an operator can open either eval run's own page without this page
 * assembling an id or reading a second route to find one.
 */
function Sides({ wire }: { wire: Wire }) {
  return (
    <DataTable
      caption="The two eval runs compared, before then after"
      rows={SIDES}
      rowLink={{
        header: "eval run id",
        leaf: (side) => wire.at(`.${side}.eval_run_id`),
        href: (side) => evalRunHref(wire.at(`.${side}.eval_run_id`).text),
      }}
      columns={[
        {
          header: "side",
          cell: (side) => (
            <span className={styles.sideName} data-testid={`side-${side}`}>
              {side}
            </span>
          ),
        },
        { header: "suite", cell: (side) => <Value leaf={wire.at(`.${side}.suite`)} /> },
        { header: "label", cell: (side) => <Value leaf={wire.at(`.${side}.label`)} /> },
        { header: "passes", cell: (side) => <Value leaf={wire.at(`.${side}.pass_count`)} /> },
        { header: "cases", cell: (side) => <Value leaf={wire.at(`.${side}.case_count`)} /> },
        { header: "provider", cell: (side) => sourceCell(wire, side, "provider") },
        { header: "model", cell: (side) => sourceCell(wire, side, "model") },
      ]}
    />
  );
}

/**
 * One side's `provider` or `model`, as served (FR-72): `unknown` with its migration where the side
 * was opened before 010, the reason keyed on this route and the side's own path.
 */
function sourceCell(wire: Wire, side: Side, field: "provider" | "model") {
  const leaf = wire.at(`.${side}.${field}`);
  return <Value leaf={leaf} nullReason={reasonIf(leaf, leaf.route, leaf.path)} />;
}

/**
 * One list of `ScoreDelta` rows: a case id and its two scores, joined by `ScorePair`'s plain `->`.
 * The row count is the response's own - the walk asks the wire for the next case id until there is
 * none - so nothing here counts, sorts or reverses anything (decision 32, `D-1`).
 */
function MovedCases({ wire, field, title }: { wire: Wire; field: string; title: string }) {
  const rows = rowsOf(wire, (index) => `.${field}[${index}].case_id`);

  return (
    <div className={styles.list} data-testid={`list-${field}`}>
      <h3 className={styles.listTitle}>{title}</h3>
      {rows.length === 0 ? (
        <Nothing field={field} />
      ) : (
        <DataTable
          caption={`${title}, in the order the gateway sent them`}
          rows={rows}
          columns={[
            {
              header: "case id",
              cell: (row) => <Value leaf={wire.at(`.${field}[${row}].case_id`)} />,
            },
            {
              header: "score before -> after",
              cell: (row) => (
                <ScorePair
                  before={wire.at(`.${field}[${row}].score_before`)}
                  after={wire.at(`.${field}[${row}].score_after`)}
                />
              ),
            },
          ]}
        />
      )}
    </div>
  );
}

/**
 * One list of bare case ids. A new or a missing case has no pair of scores to show - one side
 * never held it - so the id alone is the whole fact, rendered as the figure it is.
 */
function NamedCases({ wire, field, title }: { wire: Wire; field: string; title: string }) {
  const rows = rowsOf(wire, (index) => `.${field}[${index}]`);

  return (
    <div className={styles.list} data-testid={`list-${field}`}>
      <h3 className={styles.listTitle}>{title}</h3>
      {rows.length === 0 ? (
        <Nothing field={field} />
      ) : (
        <ul className={styles.ids}>
          {rows.map((row) => (
            <li key={row} className={styles.id}>
              <Value leaf={wire.at(`.${field}[${row}]`)} />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/**
 * What an empty list says. It is one sentence about the comparison rather than four, because the
 * fact is the comparison's and not the heading's: between these two eval runs no case moved into
 * this list. A blank would leave an operator to decide whether the list was empty or the page
 * broke, which is the one thing `D-3` will not have a screen do.
 */
function Nothing({ field }: { field: string }) {
  return (
    <p className={styles.note} data-testid={`empty-${field}`}>
      {NO_MOVEMENT}
    </p>
  );
}

/**
 * The two ids a comparison needs, where the URL carries fewer than two.
 *
 * Both sides are offered even when one is already in the URL, so a mistyped id can be replaced
 * without an operator editing a query string by hand. Each field sits in its own group named for
 * its side, because two lookups for the same kind of id are otherwise one label repeated twice.
 * Nothing is fetched here and nothing is checked: an id no ledger holds is the comparison's own
 * 404 quoting the gateway, rather than a second and quieter source of truth about what is held.
 */
function ComparePicker({ before, after }: Props) {
  const held: Record<Side, string | undefined> = { before, after };

  return (
    <div className={styles.picker}>
      <p className={styles.note} data-testid="compare-needs">
        {NEEDS_BOTH}
      </p>
      {SIDES.map((side) => (
        <fieldset key={side} className={styles.pickerSide}>
          <legend className={styles.pickerLegend}>{side}</legend>
          <IdLookup kind="eval run id" hrefFor={(id) => sideHref(side, id, held[other(side)])} />
        </fieldset>
      ))}
    </div>
  );
}

/** The side a lookup is not for, so filling one field never drops what the URL already held. */
function other(side: Side): Side {
  return side === "before" ? "after" : "before";
}
