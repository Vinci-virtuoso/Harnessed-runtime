/**
 * `/models` - which providers are configured, and what each one charges (FR-89, UJ-17).
 *
 * A server component holding no data (decision 34): every panel below fetches its own route in
 * the browser, so no gateway value is ever in the server-rendered payload. Stacked sections, each
 * its own `PanelFrame` heading and its own route (design.md "The models page"): **Providers** over
 * `/v1/providers` and **Prices** over `/v1/prices` (`priced_models` is not on the providers route,
 * decision 55; the stored prices are their own panel instead).
 *
 * **Router and agents** (FR-121, ticket 186) replaces the v4 Agents section: `RouterPanel` over
 * `/v1/router` renders `RouterCard` at `#router`, the anchor the Overview diagram's delegation box
 * links to, then `RegistryPanel` over `/v1/agents` renders one `RegistryCard` per entry.
 *
 * The page closes with `SystemSection` (FR-129), a collapsed disclosure holding the v3 health
 * panel moved off the Overview.
 */
import { HealthPanel } from "../../components/HealthPanel";
import { SystemSection } from "../../components/SystemSection";

import { PricesPanel, ProvidersPanel, RegistryPanel, RouterPanel } from "./panels";
import styles from "./models.module.css";

export default function ModelsPage() {
  return (
    <div className={styles.page}>
      <h1 className={styles.title}>Models</h1>
      <p className={styles.subtitle}>
        Providers are configured through the one path the CLI uses: a key is checked with one
        call to the provider before a byte of it is written.
      </p>
      <ProvidersPanel />
      <PricesPanel />
      <RouterPanel />
      <div id="agents">
        <RegistryPanel />
      </div>
      <SystemSection>
        <HealthPanel />
      </SystemSection>
    </div>
  );
}
