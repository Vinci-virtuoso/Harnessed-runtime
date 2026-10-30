"use client";

/**
 * The Overview's one panel: what the gateway answers about itself (FR-54).
 *
 * Three fields of one response, each a `StatTile` wrapping one `Value`, so each carries its own
 * route and jq path and opens the body it was read out of (`D-4`, MUST rule 21). Nothing here is
 * summed, counted or compared: `route_count` is the gateway's own number, rendered as the
 * characters it sent.
 *
 * **`schema_version` is the version the package ships**, not the version of the database on disk.
 * `/health` answers without opening the file (which is why UJ-12's Path C exists at all), so the
 * tile says which version it is and the gloss says which version it is not. A state that quotes
 * the expected and the actual version needs the gateway to report the on-disk one, which is
 * `[GAP G-2]`.
 *
 * The doctor ladder (`[GAP G-1]`) and the store counts (`[GAP G-15]`) have no gateway source, so
 * they are not on this page in any form: a `GapPlaceholder` is a review mark and never ships.
 */
import { PanelFrame } from "./PanelFrame";
import { StatTile } from "./StatTile";
import { HEALTH_ROUTE } from "./GatewayStatusPill";
import styles from "./HealthPanel.module.css";

export function HealthPanel() {
  return (
    <PanelFrame route={HEALTH_ROUTE} title="Gateway health" sourceLabel="Harness: this machine">
      {(wire) => (
        <div className={styles.body}>
          <div className={styles.tiles}>
            <StatTile label="schema version shipped" leaf={wire.at(".schema_version")} />
            <StatTile label="database path" leaf={wire.at(".database_path")} />
            <StatTile label="route count" leaf={wire.at(".route_count")} />
          </div>
          <p className={styles.gloss} data-testid="schema-version-gloss">
            The schema version this gateway package ships, not the version of the database on disk:
            the gateway answers this route without opening the file.
          </p>
        </div>
      )}
    </PanelFrame>
  );
}
