// @vitest-environment jsdom
/**
 * FR-86 (UJ-14, UJ-15; decision 42): a `Leaf` may travel between components, a figure may not.
 *
 * Ticket 144's criteria in executable form:
 *
 *   - the diagram's box labels - the turn count on the loop, facts on memory, the gate's split on
 *     the gate - are the Overview panels' own `Leaf`s passed down as props and rendered through
 *     `<Value>`, each keeping its `route` and `path` and opening the same drawer as its tile;
 *   - every label figure on the diagram has a source and matches the wire, by the independent
 *     oracle in `tests/support/source-at.ts`;
 *   - a label is never remembered: a panel that is not `ok` lends the diagram nothing;
 *   - `lib/wire.ts` still exports no `number` (the check itself is `wire-fidelity.test.tsx`'s; this
 *     file records the export surface as evidence).
 *
 * The static check (0 `leaf.text` lifted into a component prop, proven on a seeded violation) is in
 * `no-derivation.test.ts`, beside the rule it extends. The inputs are the recorded gateway bodies
 * (decision 33, MUST rule 24). This suite renders no pixels: its evidence is text.
 */
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import Home from "../../app/page";
import { AppShell } from "../../components/AppShell";
import { DIAGRAM_NAME, HarnessDiagram } from "../../components/HarnessDiagram";
import type { RawResponse } from "../../lib/fetch-panel";
import { parseWire } from "../../lib/wire";
import { figuresWithoutSource } from "../support/figures";
import { divergences, renderedFigures } from "../support/source-at";

vi.setConfig({ testTimeout: 20 * 1000 });

const DASHBOARD = resolve(import.meta.dirname, "../..");
const GATEWAY_FIXTURES = resolve(DASHBOARD, "tests/fixtures/gateway");
const EVIDENCE_DIR = resolve(DASHBOARD, "../evidence/144");

const FROZEN = new Date("2026-09-23T09:15:00");

function body(name: string): string {
  return readFileSync(resolve(GATEWAY_FIXTURES, `${name}.body`), "utf8");
}

/** Each label: the key the diagram draws it under, its region, and the tile's route and path. */
const LABELS = [
  { key: "turns", region: "Loop", route: "/v1/runs/totals", path: ".turns" },
  { key: "facts active", region: "Memory", route: "/v1/memory/totals", path: ".facts_active" },
  { key: "skip", region: "Memory", route: "/v1/events/gate", path: ".skip" },
  { key: "retrieve", region: "Memory", route: "/v1/events/gate", path: ".retrieve" },
  { key: "failed open", region: "Memory", route: "/v1/events/gate", path: ".failed_open" },
] as const;

const BODIES: ReadonlyMap<string, string> = new Map([
  ["/v1/spend", body("spend")],
  ["/v1/runs/turn_seconds", body("runs.turn-seconds")],
  ["/v1/runs/totals", body("runs.totals")],
  ["/v1/events/totals", body("events.totals")],
  ["/v1/events/gate", body("events.gate")],
  ["/v1/memory/totals", body("memory.totals")],
  ["/v1/health", body("health")],
  ["/v1/sessions", body("sessions.listing")],
  ["/v1/runs?limit=20", body("runs.listing")],
]);

/** Routes answered otherwise for one test. */
let overrides = new Map<string, () => Promise<Response>>();

function json(text: string, status = 200): Promise<Response> {
  return Promise.resolve(new Response(text, { status, headers: { "content-type": "application/json" } }));
}

function gateway(input: RequestInfo | URL): Promise<Response> {
  const url = String(input);
  const override = overrides.get(url);
  if (override !== undefined) return override();
  if (url.endsWith("/stream")) {
    // A stream that stays open and says nothing: the labels are read, never streamed.
    const opened = new ReadableStream<Uint8Array>({ start() {} });
    return Promise.resolve(
      new Response(opened, { status: 200, headers: { "content-type": "text/event-stream" } }),
    );
  }
  const found = BODIES.get(url);
  if (found === undefined) return json(body("notfound.route"), 404);
  return json(found);
}

async function flushed(): Promise<void> {
  for (const _ of [1, 2, 3]) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
  }
}

/** The diagram on the page: the frame holding the picture and the linked layer over it. */
function diagram(): HTMLElement {
  const picture = screen.getByRole("img", { name: DIAGRAM_NAME });
  const frame = picture.parentElement?.parentElement;
  if (!(frame instanceof HTMLElement)) throw new Error("the diagram has no frame");
  return frame;
}

function labelFigure(container: ParentNode, route: string, path: string): HTMLElement | null {
  return container.querySelector<HTMLElement>(`[data-figure][data-route="${route}"][data-path="${path}"]`);
}

/** The tile's own figure at `route`/`path`, wherever on the page it rendered outside the diagram. */
function tileFigure(route: string, path: string): HTMLElement {
  const onDiagram = diagram();
  const matches = [...document.querySelectorAll<HTMLElement>(
    `[data-figure][data-route="${route}"][data-path="${path}"]`,
  )].filter((node) => !onDiagram.contains(node));
  expect(matches, `${route} ${path}`).toHaveLength(1);
  return matches[0] as HTMLElement;
}

/** Open the drawer from `figure` - now the trigger itself - read it, and close it again. */
function drawerFrom(figure: HTMLElement): string {
  fireEvent.click(figure);
  const dialog = screen.getByRole("dialog");
  const text = dialog.textContent ?? "";
  fireEvent.click(within(dialog).getByRole("button", { name: /close/i }));
  return text;
}

const evidence: string[] = [];

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date", "setTimeout", "clearTimeout", "setInterval", "clearInterval"] });
  vi.setSystemTime(FROZEN);
  overrides = new Map();
  vi.stubGlobal("matchMedia", (query: string) => ({ matches: true, media: query }));
  vi.stubGlobal("fetch", vi.fn(gateway));
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

afterAll(() => {
  mkdirSync(EVIDENCE_DIR, { recursive: true });
  writeFileSync(resolve(EVIDENCE_DIR, "labels.txt"), `${evidence.join("\n")}\n`, "utf8");
  const wire = readFileSync(resolve(DASHBOARD, "lib/wire.ts"), "utf8");
  const exports = wire.split("\n").filter((line) => line.startsWith("export "));
  writeFileSync(
    resolve(EVIDENCE_DIR, "wire-exports.txt"),
    ["FR-86: lib/wire.ts's export surface (no `number` among them)", ...exports, ""].join("\n"),
    "utf8",
  );
});

describe("HarnessDiagram labels", () => {
  function leaves() {
    return Object.fromEntries(
      LABELS.map((label) => [label.key, parseWire(BODIES.get(label.route) ?? "", label.route).at(label.path)]),
    );
  }

  it("renders each label Leaf through Value in its region, keeping its route and path", () => {
    render(<HarnessDiagram stage={null} labels={leaves()} />);

    for (const label of LABELS) {
      const region = screen.getByRole("group", { name: label.region });
      const figure = labelFigure(region, label.route, label.path);
      expect(figure, label.key).not.toBeNull();
      expect(within(region).getByText(label.key)).toBeTruthy();
    }
    expect(figuresWithoutSource(document.body)).toEqual([]);
    expect(divergences(renderedFigures(document.body), BODIES)).toEqual([]);
  });

  it("opens the drawer over the response a label's source names, at the label's path", () => {
    const response: RawResponse = {
      url: "/v1/runs/totals",
      status: "200",
      bytes: "99",
      text: BODIES.get("/v1/runs/totals") ?? "",
      fetchedAt: FROZEN,
    };
    render(
      <HarnessDiagram stage={null} labels={leaves()} sources={{ "/v1/runs/totals": response }} />,
    );

    const figure = labelFigure(document, "/v1/runs/totals", ".turns");
    expect(figure).not.toBeNull();
    fireEvent.click(figure as HTMLElement);
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("Ledger: all time")).toBeTruthy();
    expect(within(dialog).getByTestId("field-name").textContent).toBe("turns");
    expect(within(dialog).getByTestId("exact-value").textContent).toBe("43");
    fireEvent.click(within(dialog).getByRole("button", { name: "Close" }));

    // A label whose response the diagram was not handed still names its source, and offers no
    // button that would open nothing.
    const facts = labelFigure(document, "/v1/memory/totals", ".facts_active");
    expect(facts?.parentElement?.querySelector("button")).toBeFalsy();
    expect(facts?.getAttribute("title")).toBeNull();
  });

  it("draws no label it was not handed", () => {
    render(<HarnessDiagram stage={null} labels={{}} />);
    expect(diagram().querySelectorAll("[data-figure]")).toHaveLength(0);
  });
});

describe("the Overview hands its panels' Leafs to the diagram", () => {
  it("labels the loop, memory and gate with the tiles' own figures, matching the wire", async () => {
    render(
      <AppShell>
        <Home />
      </AppShell>,
    );
    await flushed();

    const onDiagram = diagram();
    const figures = renderedFigures(onDiagram);
    expect(figures.map(({ route, path }) => `${route} ${path}`)).toEqual(
      LABELS.map(({ route, path }) => `${route} ${path}`),
    );
    expect(figuresWithoutSource(onDiagram)).toEqual([]);
    expect(divergences(figures, BODIES)).toEqual([]);

    evidence.push("FR-86, ticket 144: the diagram's labels, each a tile's Leaf, matched against the wire");
    for (const label of LABELS) {
      const onBox = labelFigure(onDiagram, label.route, label.path) as HTMLElement;
      const tile = tileFigure(label.route, label.path);
      expect(onBox.textContent, label.key).toBe(tile.textContent);

      // The same drawer: the one the tile's chip opens, over the same response, at the same path.
      const fromBox = drawerFrom(onBox);
      const fromTile = drawerFrom(tile);
      expect(fromBox, label.key).toBe(fromTile);
      evidence.push(
        `${label.region} / ${label.key}: ${onBox.textContent} <- ${label.route} ${label.path} (tile: ${tile.textContent}; same drawer: ${fromBox === fromTile})`,
      );
    }
    evidence.push(`divergences from the wire: ${divergences(figures, BODIES).length}`);
  });

  it("drops a label whose panel is not ok, rather than remembering it", async () => {
    overrides.set("/v1/memory/totals", () => json(body("notfound.route"), 500));
    render(
      <AppShell>
        <Home />
      </AppShell>,
    );
    await flushed();

    const onDiagram = diagram();
    expect(labelFigure(onDiagram, "/v1/memory/totals", ".facts_active")).toBeNull();
    expect(labelFigure(onDiagram, "/v1/runs/totals", ".turns")).not.toBeNull();
    evidence.push("memory totals answering 500: the facts label is absent, never remembered");
  });
});
