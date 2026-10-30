"use client";

/**
 * `/memory` - what the harness holds in memory, on one page (FR-58, FR-64, FR-129; UJ-10). A
 * architecture diagram, a figures row, then **Facts** (`?q=` searches, `?id=` opens one), **Procedures** (the active set)
 * and **Episodes** (`?session=` lists one session's).
 *
 * A client component holding no data (decision 34): it reads the query and nothing else, and every
 * figure below it is fetched in the browser. Every value is passed down exactly as the URL carried
 * it - no trim, no re-casing, no split - because the gateway is the only thing that takes an id or
 * an FTS expression apart (MUST rule 25). **Until the URL carries a query or a session id nothing
 * is asked of the gateway for that section**: the gateway answers `[]` for a facts search without
 * a `q` and serves episodes for one session id only, so each section is a prompt instead
 * (`[GAP G-14]`, `[GAP G-17]`). The episode lookup renders once, in the Episodes section.
 *
 * **The diagram (ticket 209).** `MemoryDiagram` draws the memory layers above the figures, lit by
 * the newest session's stream the way the Overview's is (`useWatchedStream`); its labels are the
 * figures row's own `Leaf`s, lent up with their responses (decision 42), and the row re-reads when
 * the stream reports a finished run or a memory write.
 */
import { useSearchParams } from "next/navigation";
import { useContext } from "react";

import { useLentLabels } from "../../components/diagram/Lend";
import { useAgentEntries, useWatchedStream } from "../../components/diagram/useWatchedStream";
import { ConsolidationPanel } from "../../components/ConsolidationPanel";
import { MemoryDiagram } from "../../components/MemoryDiagram";
import { GatewayOriginContext } from "../../components/PanelFrame";

import { EpisodeLookup } from "./EpisodeLookup";
import { EpisodesPanel } from "./EpisodesPanel";
import { FactPanel } from "./FactPanel";
import { FactSearch } from "./FactSearch";
import { FactsPanel } from "./FactsPanel";
import { MemoryFigures } from "./MemoryFigures";
import { ProceduresPanel } from "./ProceduresPanel";
import { NEEDS_QUERY, NEEDS_SESSION } from "./search";
import styles from "./memory.module.css";

export default function MemoryPage() {
  const query = useSearchParams();
  const q = query.get("q") ?? undefined;
  const id = query.get("id") ?? undefined;
  const session = query.get("session") ?? undefined;
  const origin = useContext(GatewayOriginContext);
  const { refresh, frames } = useWatchedStream(origin);
  const { labels, sources, lend } = useLentLabels();
  const entries = useAgentEntries(origin, refresh, "cli");

  return (
    <div className={styles.page}>
      <h1 className={styles.title}>Memory</h1>
      <p className={styles.subtitle}>
        What the harness holds: the facts it can recall, the procedures it says to itself and the
        episodes each session wrote.
      </p>
      <section className={styles.diagram} aria-label="Memory architecture" data-testid="memory-diagram">
        <MemoryDiagram labels={labels} sources={sources} entries={entries} frames={frames} />
      </section>
      <MemoryFigures lend={lend} refresh={refresh} />
      <ConsolidationPanel lend={lend} refresh={refresh} />
      <section id="facts" className={styles.section} aria-label="Facts">
        <h2 className={styles.sectionTitle}>Facts</h2>
        <p className={styles.note}>
          Semantic memory, searched as typed: every word you enter is looked for, common ones
          included, which is not the query a run builds for itself. The gateway matches, ranks and
          answers, and this page shows what it sent.
        </p>
        <FactSearch q={q} />
        {id !== undefined ? (
          <FactPanel memId={id} />
        ) : q === undefined ? (
          <p className={styles.prompt} data-testid="facts-prompt">
            {NEEDS_QUERY}
          </p>
        ) : (
          <FactsPanel q={q} />
        )}
      </section>
      <section id="procedures" className={styles.section} aria-label="Procedures">
        <h2 className={styles.sectionTitle}>Procedures</h2>
        <p className={styles.note}>
          The active procedure set, as the gateway serves it: the sentences a run will be given
          before it starts.
        </p>
        <ProceduresPanel />
      </section>
      <section id="episodes" className={styles.section} aria-label="Episodes">
        <h2 className={styles.sectionTitle}>Episodes</h2>
        <p className={styles.note}>
          What one session wrote into episodic memory, in the order it wrote it.
        </p>
        {session === undefined ? (
          <p className={styles.prompt} data-testid="episodes-prompt">
            {NEEDS_SESSION}
          </p>
        ) : (
          <EpisodesPanel sessionId={session} />
        )}
        <EpisodeLookup />
      </section>
    </div>
  );
}
