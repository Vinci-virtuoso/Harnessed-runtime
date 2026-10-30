"use client";

/**
 * The Overview's panels and the one stream that tells them to read again (FR-75, UJ-14, UJ-15).
 *
 * Six `PanelFrame`s, one per aggregate route from tickets 126 to 129 and 133, each a group of
 * `StatTile`s wrapping `Value`s of **its own** response and nothing else (MUST rule 21 as amended,
 * decision 42): no panel renders a figure another panel fetched, and no figure is summed, divided
 * or compared with another (`D-1`). Every tile says the window it counts over - "all time, counted
 * now" (decision 47) - and `PanelFrame`'s footer says when the dashboard fetched it (`D-5`).
 *
 * **SSE-for-change, fetch-to-read.** The page holds one watched stream: the newest session in the
 * listing, because there is no global stream (decision 14) and the console that would name a run
 * in flight is a later ticket's. It is opened through `lib/stream.ts`, and a frame named in
 * `REFETCH_SIGNALS` is a new `refresh` token for every panel here: one more read of each panel's
 * own route, and never a figure read out of the frame. Nothing polls: between frames no panel
 * reads anything, and only `GatewayStatusPill` keeps its `/v1/health` poll. A drop waits out
 * `lib/stream.ts`'s backoff and resumes from the last `seq` it saw; a refusal or a clean close
 * ends the watch, because reopening a closed stream on a timer would be a poll by another name.
 *
 * Frames that land in one read are one update, as they are on `StreamPanel`: the read is what
 * arrived, so a read holding a terminal frame is one refetch of every panel.
 *
 * **The diagram (FR-84 to FR-86).** Beneath the tiles, full width and in no `PanelFrame` of its
 * own, `HarnessDiagram` lights from the same watched stream's frames. Its box labels are the
 * tiles' own `Leaf`s (decision 42): a `Lend` inside a panel hands its `Leaf` up with the response
 * it was read from, and takes both back when the panel stops rendering them, so the diagram shows
 * a label exactly while its tile does, refetched with it, and never a remembered one.
 */
import { useContext } from "react";

import { Aggregate, Headlines, Secondary } from "../components/Aggregate";
import { ActivityList, activityLimitLeaf } from "../components/ActivityList";
import { Lend, useLentLabels } from "../components/diagram/Lend";
import {
  type Watch,
  useAgentEntries,
  useWatchedStream,
} from "../components/diagram/useWatchedStream";
import { HarnessDiagram } from "../components/HarnessDiagram";
import { NeedsYouList } from "../components/NeedsYouList";
import { KeyKindTag } from "../components/KeyKindTag";
import { GatewayOriginContext } from "../components/PanelFrame";
import { StatTile } from "../components/StatTile";
import { TasksTile } from "../components/TasksTile";
import { Value } from "../components/Value";
import { nullReasonFor, spendTotalReason } from "../lib/null-reasons";
import type { StreamStatus } from "../lib/stream";
import type { Leaf } from "../lib/wire";

import { rowsOf } from "../lib/rows";
import { sessionHref } from "./traces/limit";
import styles from "./overview.module.css";

/** The one sentence the gate split's third count carries, and no other count does (`D-1`). */
export const FAILED_OPEN_GLOSS =
  "failed open: the gate could not answer and memory was retrieved anyway";

/** "Recent activity" reads `/v1/messages` at this fixed limit (decision 66); no limit control
 * exists for it yet, unlike the console's own `HISTORY`. */
export const ACTIVITY_LIMIT = "20";

/** The link's query value is the listing's own id, ridden verbatim (MUST rule 25); reading `.text`
 * in this function rather than inline in the JSX keeps decision 42's lint rule about a figure
 * lifted bare into a prop, not about this. */
function sessionIdOf(session: Leaf): string {
  return session.text;
}

function watchLabel(watch: Watch): string {
  if (watch.kind === "none") return "the sessions listing holds no session to watch";
  if (watch.kind === "unanswered") return `the sessions listing did not answer (${watch.status})`;
  return watchWord(watch.status);
}

/**
 * The watch line's word for where the live read is, in the operator's terms (G-57, `V6-1`): never
 * the stream's own `closed: the stream ended` or its cursor. The exact state stays in the
 * session's drawer.
 */
function watchWord(status: StreamStatus): string {
  if (status.kind === "closed") return "finished";
  if (status.kind === "refused") return `could not connect (${status.status})`;
  if (status.kind === "reconnecting") return "reconnecting";
  return status.kind;
}

export function Overview() {
  const origin = useContext(GatewayOriginContext);
  const { refresh, session, watch, frames } = useWatchedStream(origin);
  const { labels, sources, lend } = useLentLabels();
  const entries = useAgentEntries(origin, refresh);

  return (
    <>
      {/* First on the page: the only thing here that asks for an action (FR-153). */}
      <NeedsYouList refresh={refresh} />

      <p className={styles.watch} data-testid="watched-stream">
        <span>Read again when a watched run, eval, cycle or memory write finishes.</span>
        <span className={styles.watchState}>
          {session === undefined ? null : (
            <>
              session <Value leaf={session} />
              {": "}
            </>
          )}
          {watchLabel(watch)}
        </span>
      </p>

      <div className={styles.panels}>
        <div className={styles.tasksTile}>
          <TasksTile refresh={refresh} />
        </div>

        <Aggregate route="/v1/spend" title="Spent" headlines={2} refresh={refresh}>
          {(wire) => (
            <>
              <Headlines>
                <StatTile
                  label="Total"
                  leaf={wire.at(".total")}
                  nullReason={
                    wire.at(".total").kind === "null"
                      ? spendTotalReason(wire.at(".prices_fetched_at"))
                      : undefined
                  }
                />
                <StatTile label="Currency" leaf={wire.at(".currency")} />
              </Headlines>
              {/* The v5 buckets (FR-115, decision 64), each its own served figure beside `total`:
                  the API-equivalent figure is never folded into it, and nothing here is summed. */}
              <Secondary>
                <StatTile
                  size="secondary"
                  label="Router"
                  leaf={wire.at(".router_cost")}
                  nullReason={
                    wire.at(".router_cost").kind === "null"
                      ? nullReasonFor(wire.route, ".router_cost")
                      : undefined
                  }
                />
                <StatTile
                  size="secondary"
                  label="Agents"
                  leaf={wire.at(".agent_cost")}
                  nullReason={
                    wire.at(".agent_cost").kind === "null"
                      ? nullReasonFor(wire.route, ".agent_cost")
                      : undefined
                  }
                />
                <StatTile
                  size="secondary"
                  label="API-equivalent (not in the total)"
                  leaf={wire.at(".api_equivalent")}
                  nullReason={
                    wire.at(".api_equivalent").kind === "null"
                      ? nullReasonFor(wire.route, ".api_equivalent")
                      : undefined
                  }
                />
                <StatTile size="secondary" label="Not metered" leaf={wire.at(".not_metered_turns")} />
                <StatTile size="secondary" label="Priced turns" leaf={wire.at(".priced_turns")} />
              </Secondary>
              {wire.at(".unpriced_turns").text === "0" ? null : (
                <p className={styles.gloss}>
                  unpriced turns: <Value leaf={wire.at(".unpriced_turns")} />
                </p>
              )}
            </>
          )}
        </Aggregate>

        <Aggregate route="/v1/runs/turn_seconds" title="Average turn" headlines={1} refresh={refresh}>
          {(wire) => (
            <>
              <Headlines>
                <StatTile
                  label="Seconds"
                  leaf={wire.at(".average_seconds")}
                  nullReason={nullReasonFor(wire.route, ".average_seconds")}
                />
              </Headlines>
              <Secondary>
                <StatTile size="secondary" label="Turns counted" leaf={wire.at(".turns_counted")} />
              </Secondary>
              {wire.at(".turns_excluded").text === "0" ? null : (
                <p className={styles.gloss}>
                  <Value leaf={wire.at(".turns_excluded")} /> turns were recorded before Harnessed timed them
                </p>
              )}
            </>
          )}
        </Aggregate>

        <Aggregate route="/v1/runs/totals" title="Turns and tool calls" headlines={2} refresh={refresh}>
          {(wire) => (
            <>
              <Headlines>
                <StatTile label="Turns" leaf={wire.at(".turns")} />
                <StatTile label="Tool calls" leaf={wire.at(".tool_calls")} />
              </Headlines>
              <Lend name="turns" leaf={wire.at(".turns")} to={lend} />
              <p className={styles.gloss}>
                over{" "}
                {rowsOf(wire, (index) => `.key_kinds[${index}]`).map((index) => (
                  <KeyKindTag key={index} leaf={wire.at(`.key_kinds[${index}]`)} />
                ))}
              </p>
            </>
          )}
        </Aggregate>

        <Aggregate route="/v1/events/totals" title="Events and sessions" headlines={2} refresh={refresh}>
          {(wire) => (
            <Headlines>
              <StatTile label="Events" leaf={wire.at(".events")} />
              <StatTile label="Sessions" leaf={wire.at(".sessions")} />
            </Headlines>
          )}
        </Aggregate>

        <Aggregate route="/v1/events/gate" title="Memory check" headlines={3} refresh={refresh}>
          {(wire) => (
            // Declared order, three raw counts, and `decided` never offered: its sum is visible
            // and a ratio over it is exactly what `D-1` refuses.
            <>
              <Lend name="skip" leaf={wire.at(".skip")} to={lend} />
              <Lend name="retrieve" leaf={wire.at(".retrieve")} to={lend} />
              <Lend name="failed open" leaf={wire.at(".failed_open")} to={lend} />
              <Headlines>
                <StatTile label="Skipped" leaf={wire.at(".skip")} />
                <StatTile label="Retrieved" leaf={wire.at(".retrieve")} />
                <StatTile label="Failed open" leaf={wire.at(".failed_open")} />
              </Headlines>
              <p className={styles.gloss}>{FAILED_OPEN_GLOSS}</p>
            </>
          )}
        </Aggregate>

        <Aggregate route="/v1/memory/totals" title="Memory" headlines={2} refresh={refresh}>
          {(wire) => (
            <>
              <Lend name="facts active" leaf={wire.at(".facts_active")} to={lend} />
              <Headlines>
                <StatTile label="Facts" leaf={wire.at(".facts_active")} />
                <StatTile label="Procedures" leaf={wire.at(".procedures_active")} />
              </Headlines>
              <Secondary>
                <StatTile size="secondary" label="Archived facts" leaf={wire.at(".facts_archived")} />
                <StatTile size="secondary" label="Episodes" leaf={wire.at(".episodes")} />
              </Secondary>
            </>
          )}
        </Aggregate>
      </div>

      <section className={styles.diagram} data-testid="harness-diagram">
        <HarnessDiagram stage={null} labels={labels} sources={sources} entries={entries} frames={frames} />
      </section>

      {session === undefined ? null : (
        <section className={styles.latestRun}>
          <h2 className={styles.sectionTitle}>Latest run</h2>
          <a
            className={styles.latestRunLink}
            href={sessionHref(sessionIdOf(session))}
            data-figure=""
            data-route={session.route}
            data-path={session.path}
          >
            <Value leaf={session} />
          </a>
        </section>
      )}

      <ActivityList limit={activityLimitLeaf(ACTIVITY_LIMIT)} refresh={refresh} />
    </>
  );
}
