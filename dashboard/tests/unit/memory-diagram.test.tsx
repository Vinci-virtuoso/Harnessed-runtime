// @vitest-environment jsdom
/**
 * FR-85, FR-128 (ticket 209): the Memory page draws the memory architecture, live.
 *
 *   - `MemoryDiagram` has the regions and boxes the ticket names, with their links: Skills one
 *     slot per `cli` agent up to six (0 for any other kind), the gate a diamond, the identity box
 *     0 figures and 0 links;
 *   - the edges are the ticket's list, and a stage-table name lights its boxes and edges through
 *     the highlight class, or nothing under reduced motion, with the status line naming it;
 *   - the page's panels lend their own `Leaf`s (gate split, memory totals) to the diagram, each
 *     through `Value` with its own response as source, and a figure not handed is not drawn;
 *   - the file lints clean under the no-derivation rule with every coordinate a literal, and
 *     nothing interactive sits inside the `role="img"` picture;
 *   - every name `lib/memory-stages.ts` lists is frozen in `events/names.py`.
 */
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { ESLint } from "eslint";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { NO_DERIVATION_ALLOWLIST } from "../../eslint.config.mjs";
import { LINT_CASE_TIMEOUT_MS } from "../support/timeouts";
import styles from "../../components/diagram/diagram.module.css";
import { MEMORY_DIAGRAM_NAME, MemoryDiagram } from "../../components/MemoryDiagram";
import { AppShell } from "../../components/AppShell";
import MemoryPage from "../../app/memory/page";
import { MEMORY_STAGE, memoryStageFor } from "../../lib/memory-stages";
import { frameOf, type StreamFrame } from "../../lib/stream";
import { parseWire, type Leaf } from "../../lib/wire";
import { pageAt } from "./support/query";

// jsdom has no `AnimationEvent`; without one react-dom never hears `animationend`.
vi.hoisted(() => {
  if (!("AnimationEvent" in globalThis)) {
    class AnimationEvent extends Event {
      readonly animationName: string;
      constructor(type: string, init: EventInit & { animationName?: string } = {}) {
        super(type, init);
        this.animationName = init.animationName ?? "";
      }
    }
    Object.defineProperty(globalThis, "AnimationEvent", { value: AnimationEvent, configurable: true });
  }
});

vi.mock("next/navigation", async (importOriginal) => ({
  ...((await importOriginal()) as object),
  useSearchParams: (await import("./support/query")).useSearchParams,
  useRouter: () => ({ push: () => {}, replace: () => {} }),
}));

vi.setConfig({ testTimeout: 30 * 1000 });

const DASHBOARD = resolve(import.meta.dirname, "../..");
const REPO = resolve(DASHBOARD, "..");
const FIXTURES = resolve(DASHBOARD, "tests/fixtures/gateway");
const EVIDENCE = resolve(REPO, "evidence/209");
const SOURCE = "components/MemoryDiagram.tsx";

const log: string[] = [];

function body(name: string): string {
  return readFileSync(resolve(FIXTURES, `${name}.body`), "utf8");
}

/** `/v1/agents`' own `.agent` `Leaf`s, built the way the page's hook reads them. */
function entriesFrom(names: readonly string[]): Leaf[] {
  const text = JSON.stringify({ agents: names.map((agent) => ({ agent, kind: "cli" })) });
  const wire = parseWire(text, "/v1/agents");
  return names.map((_, index) => wire.at(`.agents[${index}].agent`));
}

function framesOf(...names: string[]): StreamFrame[] {
  return names.map((name, index) =>
    frameOf({ id: String(index + 1), event: name, data: "{}" }, "/v1/sessions/x/stream"),
  );
}

function reduced(reduce: boolean): void {
  vi.stubGlobal("matchMedia", (query: string) => ({ matches: reduce, media: query }));
}

const FLASH = [styles.flashA, styles.flashB];

/** Every node and edge wearing a highlight class, as `data-edge`/`data-node` ids. */
function flashing(): string[] {
  return [...document.querySelectorAll("[data-node], [data-edge]")]
    .filter((node) => FLASH.some((name) => node.classList.contains(name)))
    .map((node) => node.getAttribute("data-edge") ?? node.getAttribute("data-node") ?? "");
}

function names(): string[] {
  return [...document.querySelectorAll("[data-node]")].map((node) => node.getAttribute("aria-label") ?? "");
}

beforeEach(() => reduced(false));

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("MemoryDiagram boxes and links", () => {
  it("draws the named boxes, each linking where the ticket says", () => {
    render(<MemoryDiagram labels={{}} entries={entriesFrom(["alpha", "bravo"])} />);
    expect(screen.getByRole("img", { name: MEMORY_DIAGRAM_NAME })).toBeTruthy();
    expect(names()).toEqual([
      "Task",
      "Router (Jev)",
      "alpha",
      "bravo",
      "Retrieval gate",
      "Semantic",
      "Episodic",
      "Improvement",
      "Procedural",
      "Identity: base instruction",
      "Retrieved context",
      "System prompt",
      "Model",
    ]);
    const href = (name: string) =>
      document.querySelector(`[data-node][aria-label="${name}"]`)?.getAttribute("href") ?? null;
    expect(href("alpha")).toBe("/models#agents");
    expect(href("Semantic")).toBe("#facts");
    expect(href("Episodic")).toBe("#episodes");
    expect(href("Procedural")).toBe("#procedures");
    expect(href("Improvement")).toBe("/evals#improvement");
    expect(href("Identity: base instruction")).toBeNull();
    expect(document.querySelector('[data-node="gate"] polygon')).not.toBeNull();
    log.push(`boxes: ${names().join(", ")}`);
  });

  it("gives the identity box 0 figures and 0 links, and captions the SOUL.md it stands in for", () => {
    const { container } = render(<MemoryDiagram labels={{}} entries={[]} />);
    const identity = document.querySelector('[data-node="identity"]') as Element;
    expect(identity.tagName.toLowerCase()).toBe("g");
    expect(identity.querySelectorAll("a, [data-figure]")).toHaveLength(0);
    expect(container.textContent).toContain("SOUL.md would sit");
  });

  it("draws one Skills slot per entry up to six, and 'more' past the sixth", () => {
    const six = entriesFrom(["a", "b", "c", "d", "e", "f"]);
    const { unmount } = render(<MemoryDiagram labels={{}} entries={six} />);
    expect(document.querySelectorAll('[data-node^="skill"]')).toHaveLength(6);
    expect(screen.queryByText("more on the Models page")).toBeNull();
    unmount();
    render(<MemoryDiagram labels={{}} entries={entriesFrom(["a", "b", "c", "d", "e", "f", "g"])} />);
    expect(document.querySelectorAll('[data-node^="skill"]')).toHaveLength(6);
    expect(screen.getByText("more on the Models page")).toBeTruthy();
  });

  it("draws exactly the ticket's edges", () => {
    render(<MemoryDiagram labels={{}} entries={[]} />);
    const edges = [...document.querySelectorAll("[data-edge]")].map((edge) => edge.getAttribute("data-edge"));
    expect(edges).toEqual([
      "task-router",
      "router-skills",
      "router-gate",
      "gate-semantic",
      "gate-episodic",
      "semantic-retrieved",
      "episodic-retrieved",
      "retrieved-model",
      "procedural-system",
      "identity-system",
      "system-model",
      "model-episodic",
      "episodic-semantic",
      "improve-procedural",
    ]);
    expect(document.querySelector('[data-edge="router-skills"]')?.classList.contains(styles.dashed)).toBe(true);
    log.push(`edges: ${edges.join(", ")}`);
  });

  it("holds nothing interactive inside the picture (0 nested-interactive)", () => {
    render(<MemoryDiagram labels={{}} entries={entriesFrom(["alpha"])} />);
    const picture = screen.getByRole("img", { name: MEMORY_DIAGRAM_NAME });
    expect(picture.querySelectorAll("a, button, [tabindex], [data-figure]")).toHaveLength(0);
  });
});

describe("MemoryDiagram lights on the stream", () => {
  it.each(Object.keys(MEMORY_STAGE))("%s lights its stage and the status line names it", (name) => {
    render(<MemoryDiagram labels={{}} frames={framesOf(name)} />);
    const stage = memoryStageFor(name);
    expect(flashing().sort()).toEqual([...stage.edges, ...stage.nodes].sort());
    expect(screen.getByRole("status").textContent).toMatch(/^Last step: .+, /);
    expect(document.querySelector('[data-node="gate"]')?.getAttribute("data-lit")).toBe(
      stage.warn ? "warn" : null,
    );
  });

  it("shows the word beside the gate on failed open, and lights the next frame after the beat", () => {
    render(<MemoryDiagram labels={{}} frames={framesOf("retrieval.gate.failed_open", "retrieval.budget.truncated")} />);
    expect(screen.getByText("failed open")).toBeTruthy();
    expect(flashing()).toContain("gate");
    act(() => {
      fireEvent.animationEnd(document.querySelector("[data-beat]") as Element);
    });
    expect(screen.queryByText("failed open")).toBeNull();
    expect(flashing()).toEqual(["retrieved"]);
  });

  it("lights nothing for a name it has no row for, and still names the step", () => {
    render(<MemoryDiagram labels={{}} frames={framesOf("tool.call.started")} />);
    expect(flashing()).toEqual([]);
    expect(screen.getByRole("status").textContent).toMatch(/^Last step: /);
  });

  it("lights nothing under prefers-reduced-motion, and the status line still names the step", () => {
    reduced(true);
    render(<MemoryDiagram labels={{}} frames={framesOf("retrieval.assembled")} />);
    expect(flashing()).toEqual([]);
    expect(document.querySelector("[data-beat]")).toBeNull();
    expect(screen.getByRole("status").textContent).toMatch(/^Last step: .+, /);
  });

  it("says Idle before any frame", () => {
    render(<MemoryDiagram labels={{}} />);
    expect(screen.getByRole("status").textContent).toBe("Idle");
  });
});

// ---- the page: labels are the panels' own Leafs, agents are the cli ones -----------------------

const AGENTS = JSON.stringify({
  agents: [
    { agent: "codex", kind: "cli" },
    { agent: "zed", kind: "acp" },
    { agent: "gemini", kind: "cli" },
  ],
});

function gateway(input: RequestInfo | URL): Promise<Response> {
  const url = String(input);
  const json = (text: string) =>
    Promise.resolve(new Response(text, { status: 200, headers: { "content-type": "application/json" } }));
  if (url.endsWith("/stream")) {
    const opened = new ReadableStream<Uint8Array>({ start() {} });
    return Promise.resolve(new Response(opened, { status: 200, headers: { "content-type": "text/event-stream" } }));
  }
  if (url.includes("/v1/agents")) return json(AGENTS);
  if (url.includes("/v1/sessions")) return json(body("sessions.listing"));
  if (url.includes("/v1/events/gate")) return json(body("events.gate"));
  if (url.includes("/v1/memory/totals")) return json(body("memory.totals"));
  return Promise.resolve(new Response("{}", { status: 404 }));
}

async function settle(): Promise<void> {
  for (const _ of [1, 2, 3, 4]) {
    await act(async () => {
      await new Promise((done) => setTimeout(done, 0));
    });
  }
}

async function renderPage() {
  vi.stubGlobal("fetch", vi.fn(gateway));
  render(<AppShell>{pageAt(MemoryPage)}</AppShell>);
  await settle();
  return screen.getByTestId("memory-diagram");
}

describe("the Memory page's diagram", () => {
  it("sits above the figures row, and draws only the cli agents", async () => {
    const diagram = await renderPage();
    const figures = screen.getByRole("region", { name: "Memory check" });
    expect(diagram.compareDocumentPosition(figures) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    const skills = [...diagram.querySelectorAll('[data-node^="skill"]')].map((box) => box.getAttribute("aria-label"));
    expect(skills).toEqual(["codex", "gemini"]);
    expect(within(diagram).queryByLabelText("zed")).toBeNull();
    log.push(`skills drawn: ${skills.join(", ")} (acp entry drawn: 0)`);
  });

  it("labels the boxes with the panels' own figures, through Value with their responses", async () => {
    const diagram = await renderPage();
    const LABELS = [
      { route: "/v1/events/gate", path: ".skip" },
      { route: "/v1/events/gate", path: ".retrieve" },
      { route: "/v1/events/gate", path: ".failed_open" },
      { route: "/v1/memory/totals", path: ".facts_active" },
      { route: "/v1/memory/totals", path: ".facts_archived" },
      { route: "/v1/memory/totals", path: ".episodes" },
      { route: "/v1/memory/totals", path: ".procedures_active" },
    ];
    expect(diagram.querySelectorAll("[data-figure]")).toHaveLength(LABELS.length);
    for (const { route, path } of LABELS) {
      const selector = `[data-figure][data-route="${route}"][data-path="${path}"]`;
      const onDiagram = diagram.querySelector<HTMLElement>(selector);
      const tiles = [...document.querySelectorAll<HTMLElement>(selector)].filter((node) => !diagram.contains(node));
      expect(tiles, path).toHaveLength(1);
      expect(onDiagram?.textContent, path).toBe(tiles[0].textContent);
    }
    // Each label's chip opens the drawer over its own response.
    const skip = diagram.querySelector<HTMLElement>('[data-figure][data-path=".skip"]') as HTMLElement;
    fireEvent.click(skip);
    expect(screen.getByRole("dialog").textContent).toContain("harnessed events gate");
    log.push(`labels on the diagram: ${LABELS.length}, each equal to its tile`);
  });

  it("draws no figure it was not handed", () => {
    const { container } = render(<MemoryDiagram labels={{}} entries={[]} />);
    expect(container.querySelectorAll("[data-figure]")).toHaveLength(0);
  });
});

// ---- geometry and drift ------------------------------------------------------------------------

describe("MemoryDiagram geometry", () => {
  // A cold type-aware ESLint start outruns vitest's 5 s default on slow hosts (ticket 228): the shared lint-case budget.
  it("passes the no-derivation lint with no allowlist entry, and every coordinate is a literal", { timeout: LINT_CASE_TIMEOUT_MS }, async () => {
    const eslint = new ESLint({ cwd: DASHBOARD });
    const results = await eslint.lintFiles([SOURCE, "lib/memory-stages.ts"]);
    const found = results.flatMap((result) =>
      result.messages.filter((message) => message.ruleId === "no-restricted-syntax").map((m) => `${m.line} ${m.message}`),
    );
    expect(found).toEqual([]);
    expect(NO_DERIVATION_ALLOWLIST).toEqual(["lib/clock.ts", "lib/stream.ts", "lib/humanize.ts"]);

    const source = readFileSync(resolve(DASHBOARD, SOURCE), "utf8");
    const attribute = /\s(x|y|x1|y1|x2|y2|cx|cy|r|rx|ry|width|height|points|d|viewBox)=(\{[^}]*\}|"[^"]*")/g;
    const coordinates = [...source.matchAll(attribute)];
    const derived = coordinates.filter(
      ([, , value]) => !/^"[\d .,A-Za-z-]*"$/.test(value) && !/^\{\d+\}$/.test(value),
    );
    expect(coordinates.length).toBeGreaterThan(40);
    expect(derived.map(([whole]) => whole)).toEqual([]);
    log.push(`lint: 0 findings; allowlist unchanged; ${coordinates.length} coordinates, ${derived.length} not literal`);
  });
});

describe("lib/memory-stages.ts drift", () => {
  it("lists only names frozen in events/names.py", () => {
    const python = readFileSync(resolve(REPO, "src/harnessed/events/names.py"), "utf8");
    const frozen = new Set([...python.matchAll(/^\s+"([a-z_.]+)",/gm)].map(([, name]) => name));
    expect(frozen.size).toBeGreaterThan(20);
    const source = readFileSync(resolve(DASHBOARD, "lib/memory-stages.ts"), "utf8");
    const rows = [...source.matchAll(/^\s+"([a-z_.]+)":\s/gm)].map(([, name]) => name);
    expect(rows.sort()).toEqual(Object.keys(MEMORY_STAGE).sort());
    expect(rows.filter((name) => !frozen.has(name))).toEqual([]);
    log.push(`stage rows: ${rows.length}, all frozen`);
  });
});

afterEach(() => {
  mkdirSync(EVIDENCE, { recursive: true });
  writeFileSync(resolve(EVIDENCE, "memory-diagram.txt"), [...log, ""].join("\n"), "utf8");
});

describe("the consolidation arrow (ticket 254, FR-155)", () => {
  it("runs from Episodic to Semantic on the spine's coordinates, captioned and at rest", () => {
    render(<MemoryDiagram labels={{}} />);
    const edge = document.querySelector('[data-edge="episodic-semantic"]');
    expect(edge?.getAttribute("d")).toBe("M600 384 H584 V232 H600");
    expect(screen.getByText("consolidates").getAttribute("x")).toBe("580");
    expect(flashing()).toEqual([]);
  });

  it("lights Episodic, Semantic and the edge only for memory.consolidation.finished", () => {
    render(<MemoryDiagram labels={{}} frames={framesOf("memory.consolidation.finished")} />);
    expect(flashing().sort()).toEqual(["episodic", "episodic-semantic", "semantic"]);
    cleanup();
    render(<MemoryDiagram labels={{}} frames={framesOf("run.started")} />);
    expect(flashing()).toEqual([]);
  });

  it("lights nothing under reduced motion", () => {
    reduced(true);
    render(<MemoryDiagram labels={{}} frames={framesOf("memory.consolidation.finished")} />);
    expect(flashing()).toEqual([]);
  });

  it("reads No consolidation yet without the panel's leaves", () => {
    render(<MemoryDiagram labels={{}} />);
    expect(screen.getByTestId("consolidation-status").textContent).toBe("No consolidation yet");
  });
});
