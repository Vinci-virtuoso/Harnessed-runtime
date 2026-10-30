"use client";

/**
 * The Models page's two panels (FR-89, decision 55): one request each, one state each.
 *
 * **Providers** renders `GET /v1/providers` as the gateway states it: one `ProviderCard` per row,
 * in the response's order, each figure a `Leaf` of that response (MUST rule 21). A saved key is
 * the one write on this page, and it goes through the card's form to the control app; when the
 * control app says `200` the panel reads its route again, so the new `configured` is the gateway's
 * word and never one this page assumed. A refusal reads nothing again: nothing was written.
 *
 * **Prices** renders `GET /v1/prices`: the stored rows, every rate the decimal string the refresh
 * wrote and each row with its own `fetched_at`. Zero rows is design.md's sentence for "no prices
 * fetched", which names the one command that asks a provider; this page never asks one itself.
 *
 * **Router and agents** (FR-121, ticket 186) replaces the v4 Agents section: `RouterPanel` over
 * `GET /v1/router` renders one `RouterCard` first, then `RegistryPanel` over `GET /v1/agents`
 * renders one `RegistryCard` per declared agent, in the registry's order, each the newest
 * recorded probe and nothing this page ran (`RegistryCard` replaces `AgentCard`). Zero registry
 * rows is design.md's sentence for "no `agents.toml` entries", which names the command that adds
 * one; the router route never answers an empty collection, so `RouterPanel` needs no empty text.
 */
import { useId, useState } from "react";

import { PanelFrame } from "../../components/PanelFrame";
import { ProviderCard } from "../../components/ProviderCard";
import { RegistryCard } from "../../components/RegistryCard";
import { RouterCard } from "../../components/RouterCard";
import { Value } from "../../components/Value";
import type { ControlResult } from "../../lib/control-client";
import { NO_AGENTS, NO_PRICES, nullReasonFor } from "../../lib/null-reasons";
import type { Wire } from "../../lib/wire";

import { rowsOf } from "../../lib/rows";
import styles from "./models.module.css";

export const PROVIDERS_ROUTE = "/v1/providers";
export const PRICES_ROUTE = "/v1/prices";
export const ROUTER_ROUTE = "/v1/router";
export const AGENTS_ROUTE = "/v1/agents";

/** Every section's heading, so a test and a screen reader find them by the same words. */
export const PROVIDERS_TITLE = "Providers";
export const PRICES_TITLE = "Prices";
export const ROUTER_TITLE = "Router";
export const AGENTS_TITLE = "Agents";

export function ProvidersPanel() {
  // A new object is one more read of the route, exactly as a `Refetch` press is (PanelFrame).
  const [refresh, setRefresh] = useState<object | undefined>(undefined);
  const onAnswered = (result: ControlResult) => {
    if (result.state === "ok") setRefresh({});
  };

  return (
    <PanelFrame
      route={PROVIDERS_ROUTE}
      title={PROVIDERS_TITLE}
      sourceLabel="Providers: this machine"
      refresh={refresh}
    >
      {(wire) => {
        const rows = rowsOf(wire, (index) => `.providers[${index}].name`);
        if (rows.length === 0) return null;
        return (
          <ul className={styles.grid}>
            {rows.map((row) => (
              <li key={row} className={styles.item}>
                <ProviderCard
                  name={provider(wire, row, "name")}
                  configured={provider(wire, row, "configured")}
                  keyVar={provider(wire, row, "key_var")}
                  defaultModel={provider(wire, row, "default_model")}
                  onAnswered={onAnswered}
                />
              </li>
            ))}
          </ul>
        );
      }}
    </PanelFrame>
  );
}

/** One field of one provider row, as a leaf of this panel's own response. */
function provider(wire: Wire, row: number, field: string) {
  return wire.at(`.providers[${row}].${field}`);
}

/** The stored price fields, named as the wire names them, in the order a reader compares them. */
const PRICE_FIELDS = [
  "provider",
  "model",
  "input",
  "output",
  "cached_input",
  "currency",
  "fetched_at",
  "source_url",
] as const;

export const PRICES_SEARCH_LABEL = "Search models";
export const PRICES_HINT = "Type a model name to see its stored price.";
export const PRICES_NO_MATCH = "No stored price matches.";

export function PricesPanel() {
  const [query, setQuery] = useState("");
  const listId = useId();
  return (
    <PanelFrame
      route={PRICES_ROUTE}
      title={PRICES_TITLE}
      sourceLabel="Prices: this machine"
      emptyText={NO_PRICES}
    >
      {(wire) => {
        const rows = rowsOf(wire, (index) => `.prices[${index}].model`);
        if (rows.length === 0) return null;
        const needle = query.toLowerCase();
        // A name search narrows what is shown; the rows keep the order the gateway sent.
        const shown = rows.filter((row) =>
          wire.at(`.prices[${row}].model`).text.toLowerCase().includes(needle),
        );
        return (
          <div className={styles.search}>
            <label className={styles.searchLabel}>
              {PRICES_SEARCH_LABEL}
              <input
                type="search"
                list={listId}
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                className={styles.searchInput}
              />
            </label>
            <datalist id={listId}>
              {rows.map((row) => {
                const model = wire.at(`.prices[${row}].model`).text;
                return <option key={row} value={model} />;
              })}
            </datalist>
            {query === "" ? (
              <p className={styles.searchNote}>{PRICES_HINT}</p>
            ) : shown.length === 0 ? (
              <p className={styles.searchNote}>{PRICES_NO_MATCH}</p>
            ) : (
              <ul className={styles.grid}>
                {shown.map((row) => (
                  <li key={row} className={styles.priceCard}>
                    {PRICE_FIELDS.map((field) => {
                      const leaf = wire.at(`.prices[${row}].${field}`);
                      return (
                        <div key={field} className={styles.priceField}>
                          <span className={styles.priceName}>{field}</span>
                          <Value leaf={leaf} nullReason={nullReasonFor(leaf.route, leaf.path)} />
                        </div>
                      );
                    })}
                  </li>
                ))}
              </ul>
            )}
          </div>
        );
      }}
    </PanelFrame>
  );
}

/** `wire`, scoped to one row's prefix (e.g. `.agents[0]`): a `Wire` subtree travels like a `Leaf`
 * (decision 42), so `RegistryCard` reads its own fields off it rather than a prop per field. */
function rowWire(wire: Wire, prefix: string): Wire {
  return {
    route: wire.route,
    text: wire.text,
    byteLength: wire.byteLength,
    at: (path: string) => wire.at(`${prefix}${path}`),
  };
}

export function RouterPanel() {
  return (
    <PanelFrame route={ROUTER_ROUTE} title={ROUTER_TITLE} sourceLabel="Router: this machine">
      {(wire) => <RouterCard router={wire} />}
    </PanelFrame>
  );
}

export function RegistryPanel() {
  return (
    <PanelFrame
      route={AGENTS_ROUTE}
      title={AGENTS_TITLE}
      sourceLabel="Registry: agents.toml"
      emptyText={NO_AGENTS}
    >
      {(wire) => {
        const rows = rowsOf(wire, (index) => `.agents[${index}].agent`);
        if (rows.length === 0) return null;
        return (
          <ul className={styles.grid}>
            {rows.map((row) => (
              <li key={row} className={styles.item}>
                <RegistryCard entry={rowWire(wire, `.agents[${row}]`)} />
              </li>
            ))}
          </ul>
        );
      }}
    </PanelFrame>
  );
}
