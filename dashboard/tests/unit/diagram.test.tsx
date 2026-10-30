// @vitest-environment jsdom
/**
 * FR-84: the architecture diagram, drawn from literals, in six regions whose boxes link.
 *
 * Ticket 142's component criteria in executable form (`ux/design.md`, "The architecture diagram"),
 * as ticket 190 (FR-124) widened them to v5's six regions:
 *
 *   - the no-derivation lint over `HarnessDiagram` and `lib/stages.ts` reports **0** findings with
 *     **0** new allowlist entries, and every coordinate attribute in the source is a literal;
 *   - six regions with their named boxes, `delegation` drawing one box per registry entry it was
 *     handed, in six literal slots, "more on the Models page" past the sixth;
 *   - each box is an `<a>` to its v3 page, the gate at `/memory#procedures` (`[GAP G-27]`), the
 *     router and every delegation entry at the Models page's router card (`/models#router`,
 *     ticket 186, decision 67); the CLI and You have no page of their own;
 *   - `retrieval.gate.decided` maps to the gate and all three pillars on `retrieve`, the gate alone
 *     on `skip`, and a failed-open decision to the gate in the `warn` treatment with the word;
 *   - `loop.route.decided` maps to the router toward the loop or the delegation region by outcome
 *     and handler, or to the router in the `warn` treatment with a word, on an uncertain stop or a
 *     fallback (FR-124);
 *   - the drawing is `role="img"` with an accessible name, the status line is text, and every box
 *     takes keyboard focus.
 *
 * Ticket 143's (FR-85), in the same file because its subject is the same component (decision 30b):
 *
 *   - a frame on the watched stream adds the highlight class to its stage's nodes and edges, the
 *     animation is on opacity and stroke only at `motion-highlight`, it removes itself on
 *     `animationend`, and the component holds **0** timers of its own;
 *   - under `prefers-reduced-motion: reduce` **0** animations run and the status line still names
 *     the stage in text: the text changes, the class does not;
 *   - a burst lights in `seq` order, each frame holding until its highlight ends;
 *   - a box that has never lit is at rest, identically to every box before its first frame;
 *   - fed from `lib/stream.ts`'s own reader over the recorded stream, the diagram lights what the
 *     watched stream says and nothing when the stream refuses or drops before a frame.
 *
 * Ticket 190's own (FR-124): the status line reads "Idle" before any frame, else "Last step:
 * <word>, N min ago" - the newest frame's own trace-step word and how long ago the browser
 * received it, on the dashboard's clock. This replaces the batched "N events arrived ..." sentence
 * `lib/stream.ts::announcementFor` built (`components/useAnnouncement.ts`, both unchanged and
 * still what `StreamPanel` says elsewhere) - the diagram's own line no longer uses either.
 *
 * The stage table's drift against `events/names.py` is the Python half of `verify`
 * (`tests/test_dashboard_stages.py`). This suite renders no pixels.
 */
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { useEffect, useState } from "react";
import { ESLint } from "eslint";
import { afterEach, describe, expect, it, vi } from "vitest";

import { NO_DERIVATION_ALLOWLIST } from "../../eslint.config.mjs";
import { LINT_CASE_TIMEOUT_MS } from "../support/timeouts";
import { DIAGRAM_NAME, HarnessDiagram } from "../../components/HarnessDiagram";
import styles from "../../components/diagram/diagram.module.css";
import { STEP_KIND_LABEL } from "../../components/StepIcon";
import {
  GATE_DECIDED,
  NOTHING,
  ROUTE_DECIDED,
  STAGE,
  type Stage,
  stageFor,
} from "../../lib/stages";
import { frameOf, parseFrames, readStream, type StreamFrame } from "../../lib/stream";
import { traceStepFor } from "../../lib/trace-steps";
import { parseWire, type Leaf } from "../../lib/wire";

// jsdom has no `AnimationEvent`, and without one react-dom listens for `webkitAnimationEnd` in
// place of `animationend`, so `onAnimationEnd` would never hear the event a browser sends. Give
// the window the constructor every target browser has, before react-dom is loaded.
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

const DASHBOARD = resolve(import.meta.dirname, "../..");
const EVIDENCE = resolve(DASHBOARD, "../evidence/142");
const EVIDENCE_190 = resolve(DASHBOARD, "../evidence/190");
const SOURCES = ["components/HarnessDiagram.tsx", "lib/stages.ts"];

/** A registry entry's own `.agent` `Leaf`s, built the way `/v1/agents` would serve them. */
function entriesFrom(names: readonly string[]): Leaf[] {
  const body = JSON.stringify({ agents: names.map((agent) => ({ agent })) });
  const wire = parseWire(body, "/v1/agents");
  return names.map((_, index) => wire.at(`.agents[${index}].agent`));
}

/** Three entries, used everywhere a rendered delegation box is needed but the count is not
 * itself under test. */
const ENTRIES = ["alpha", "bravo", "charlie"] as const;
const ENTRY_LEAVES = entriesFrom(ENTRIES);

/** The six regions, left to right, and the boxes each holds (design.md's v5 diagram). */
const REGIONS: readonly (readonly [string, readonly string[]])[] = [
  ["Entry", ["console", "CLI"]],
  ["Router", ["Jev routes", "You"]],
  ["Loop", ["model", "tools"]],
  ["Memory", ["gate", "procedural", "semantic", "episodic"]],
  ["Evals and the flywheel", ["eval", "improve"]],
  ["Delegation", [...ENTRIES]],
];

/** Where each box goes, as design.md maps them. The CLI and You have no page, so link to nothing. */
const HREFS: Record<string, string | null> = {
  console: "/console",
  CLI: null,
  "Jev routes": "/models#router",
  You: null,
  model: "/traces",
  tools: "/traces",
  gate: "/memory#procedures",
  procedural: "/memory#procedures",
  semantic: "/memory#facts",
  episodic: "/memory#episodes",
  eval: "/evals",
  improve: "/evals#improvement",
  alpha: "/models#router",
  bravo: "/models#router",
  charlie: "/models#router",
};

function record(name: string, lines: string[]): void {
  mkdirSync(EVIDENCE, { recursive: true });
  writeFileSync(resolve(EVIDENCE, name), [...lines, ""].join("\n"), "utf8");
}

function record190(name: string, lines: string[]): void {
  mkdirSync(EVIDENCE_190, { recursive: true });
  writeFileSync(resolve(EVIDENCE_190, name), [...lines, ""].join("\n"), "utf8");
}

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

const FIXTURES = resolve(DASHBOARD, "tests/fixtures/gateway");
const EVIDENCE_143 = resolve(DASHBOARD, "../evidence/143");

/** The recorded stream, as `lib/stream.ts` parses it: seven frames, `seq` 1 to 7. */
function recordedFrames(): readonly StreamFrame[] {
  const meta = JSON.parse(readFileSync(resolve(FIXTURES, "sessions.stream.meta.json"), "utf8")) as {
    route: string;
  };
  const sse = readFileSync(resolve(FIXTURES, "sessions.stream.sse"), "utf8").replaceAll("\r\n", "\n");
  return parseFrames(sse).frames.map((raw) => frameOf(raw, meta.route));
}

/** The operator's motion preference, as `matchMedia` answers it. */
function prefersReducedMotion(reduce: boolean): void {
  vi.stubGlobal("matchMedia", (query: string) => ({ matches: reduce, media: query }));
}

const FLASH = [styles.flashA, styles.flashB];

/** Every node and edge wearing a highlight class, as `data-edge`/`data-node` ids. */
function flashing(): string[] {
  return [...document.querySelectorAll("[data-node], [data-edge]")]
    .filter((node) => FLASH.some((name) => node.classList.contains(name)))
    .map((node) => node.getAttribute("data-edge") ?? node.getAttribute("data-node") ?? "");
}

/** The ids a stage lights, in the order `flashing()` walks the document: edges, then nodes. */
function idsOf(stage: Stage): string[] {
  return [...stage.edges, ...stage.nodes];
}

/** The one element whose `animationend` ends a frame's hold. */
function beat(): Element | null {
  return document.querySelector("[data-beat]");
}

function record143(name: string, lines: string[]): void {
  mkdirSync(EVIDENCE_143, { recursive: true });
  writeFileSync(resolve(EVIDENCE_143, name), [...lines, ""].join("\n"), "utf8");
}

/** The independent oracle for the status line's word (ticket 190): the same table
 * `components/HarnessDiagram.tsx::stepWord` reads, read again here rather than imported. */
function wordFor(frame: StreamFrame): string {
  const kind = traceStepFor(frame.name.text)?.kind(frame.payload);
  if (kind === undefined) return frame.name.text;
  const label = STEP_KIND_LABEL[kind];
  return `${label.slice(0, 1).toUpperCase()}${label.slice(1)}`;
}

describe("HarnessDiagram geometry", () => {
  it(
    "passes the no-derivation lint with no allowlist entry, and every coordinate is a literal",
    async () => {
      const eslint = new ESLint({ cwd: DASHBOARD });
      const results = await eslint.lintFiles(SOURCES);
      const found = results.flatMap((result) =>
        result.messages
          .filter((message) => message.ruleId === "no-restricted-syntax")
          .map((message) => `${result.filePath}:${message.line} ${message.message}`),
      );
      expect(found).toEqual([]);
      expect(NO_DERIVATION_ALLOWLIST).toEqual(["lib/clock.ts", "lib/stream.ts", "lib/humanize.ts"]);

      // A coordinate is a quoted literal or a braced number, never an expression.
      const source = readFileSync(resolve(DASHBOARD, SOURCES[0]), "utf8");
      const attribute = /\s(x|y|x1|y1|x2|y2|cx|cy|r|rx|ry|width|height|points|d|viewBox)=(\{[^}]*\}|"[^"]*")/g;
      const coordinates = [...source.matchAll(attribute)];
      const derived = coordinates.filter(
        ([, , value]) => !/^"[\d .,A-Za-z-]*"$/.test(value) && !/^\{\d+\}$/.test(value),
      );
      expect(coordinates.length).toBeGreaterThan(40);
      expect(derived.map(([whole]) => whole)).toEqual([]);

      record("lint.txt", [
        "FR-84, ticket 142: the no-derivation lint over the diagram",
        ...SOURCES.map((file) => `linted: ${file}`),
        `no-restricted-syntax findings: ${found.length}`,
        `allowlist: ${JSON.stringify(NO_DERIVATION_ALLOWLIST)} (unchanged, 0 new entries)`,
        `coordinate attributes in the component: ${coordinates.length}; not a literal: ${derived.length}`,
      ]);
    },
    LINT_CASE_TIMEOUT_MS, // builds ESLint over the sources: cold start outruns the 5 s default (ticket 228)
  );
});

describe("HarnessDiagram regions and links", () => {
  it("renders six regions, left to right, with their named boxes", () => {
    render(<HarnessDiagram stage={null} labels={{}} entries={ENTRY_LEAVES} />);

    const groups = screen.getAllByRole("group");
    expect(groups.map((group) => group.getAttribute("aria-label"))).toEqual(
      REGIONS.map(([region]) => region),
    );
    for (const [index, [, boxes]] of REGIONS.entries()) {
      const named = [...groups[index].querySelectorAll("[data-node]")].map((node) =>
        node.getAttribute("aria-label"),
      );
      expect(named).toEqual(boxes);
    }

    // The region captions are drawn too, in the picture itself.
    const picture = screen.getByRole("img", { name: DIAGRAM_NAME });
    for (const [region] of REGIONS) expect(within(picture).getByText(region)).toBeTruthy();

    record(
      "regions.txt",
      REGIONS.map(([region, boxes]) => `${region}: ${boxes.join(", ")}`),
    );
  });

  it("makes every box a link to its v3 page, the gate at procedures and the router and entries at router", () => {
    render(<HarnessDiagram stage={null} labels={{}} entries={ENTRY_LEAVES} />);

    const lines: string[] = [];
    for (const [box, href] of Object.entries(HREFS)) {
      const node = document.querySelector(`[data-node][aria-label="${box}"]`);
      expect(node, box).not.toBeNull();
      if (href === null) {
        expect(node?.tagName.toLowerCase(), box).not.toBe("a");
        lines.push(`${box}: no page, not a link`);
      } else {
        expect(node?.tagName.toLowerCase(), box).toBe("a");
        expect(node?.getAttribute("href"), box).toBe(href);
        lines.push(`${box} -> ${href}`);
      }
    }
    record("links.txt", ["FR-84, ticket 142: where each box goes", ...lines]);
  });

  it("draws the first six entries in served order and names the rest on the Models page", () => {
    const seven = entriesFrom(["a1", "a2", "a3", "a4", "a5", "a6", "a7"]);
    render(<HarnessDiagram stage={null} labels={{}} entries={seven} />);

    const delegation = screen.getByRole("group", { name: "Delegation" });
    const named = [...delegation.querySelectorAll("[data-node]")].map((node) => node.getAttribute("aria-label"));
    expect(named).toEqual(["a1", "a2", "a3", "a4", "a5", "a6"]);
    expect(within(delegation).getByText("more on the Models page")).toBeTruthy();

    record190("overflow.txt", [
      "FR-124: seven registry entries draw six boxes, served order, and the overflow line",
      `boxes: ${named.join(", ")}`,
      "overflow line: more on the Models page",
    ]);
  });

  it("draws no overflow line and no missing-slot box for six entries or fewer", () => {
    render(<HarnessDiagram stage={null} labels={{}} entries={ENTRY_LEAVES} />);
    const delegation = screen.getByRole("group", { name: "Delegation" });
    expect(within(delegation).queryByText("more on the Models page")).toBeNull();
  });

  it("draws nothing in the delegation region before /v1/agents answers", () => {
    render(<HarnessDiagram stage={null} labels={{}} />);
    const delegation = screen.getByRole("group", { name: "Delegation" });
    expect(delegation.querySelectorAll("[data-node]")).toHaveLength(0);
  });
});

describe("the STAGE table", () => {
  it("lights the gate and all three pillars on retrieve, the gate alone on skip", () => {
    const retrieve = stageFor(GATE_DECIDED, "retrieve");
    expect(retrieve?.nodes).toEqual(["gate", "procedural", "semantic", "episodic"]);
    expect(retrieve?.edges).toEqual(["gate-procedural", "gate-semantic", "gate-episodic"]);
    expect(retrieve?.warn).toBe(false);

    const skip = stageFor(GATE_DECIDED, "skip");
    expect(skip).toEqual({ nodes: ["gate"], edges: [], warn: false });

    const failedOpen = stageFor(GATE_DECIDED, "failed_open");
    expect(failedOpen?.nodes).toEqual(["gate"]);
    expect(failedOpen?.warn).toBe(true);
    expect(stageFor("retrieval.gate.failed_open")).toEqual(failedOpen);

    // With no decision the row is the name's own; a decision on any other name changes nothing.
    expect(stageFor(GATE_DECIDED)).toEqual(STAGE[GATE_DECIDED]);
    expect(stageFor("run.started", "retrieve")).toEqual(STAGE["run.started"]);
    expect(stageFor("not.a.frozen.name")).toBeUndefined();

    // The names that light nothing have a row that says so, rather than no row.
    for (const name of ["loop.guardrail.tripped", "error.raised", "observer.failed"]) {
      expect(stageFor(name), name).toBe(NOTHING);
    }
  });

  it("lights the router toward the loop or the delegation region, by outcome and handler (FR-124)", () => {
    const groq = stageFor(ROUTE_DECIDED, undefined, "routed_groq");
    expect(groq).toEqual({ nodes: ["router"], edges: ["router-loop"], warn: false });

    const agent = stageFor(ROUTE_DECIDED, undefined, "routed_agent");
    expect(agent).toEqual({ nodes: ["router"], edges: ["router-delegation"], warn: false });

    const uncertain = stageFor(ROUTE_DECIDED, undefined, "uncertain");
    expect(uncertain).toEqual({ nodes: ["router"], edges: ["router-you"], warn: true, word: "uncertain" });

    const fallback = stageFor(ROUTE_DECIDED, undefined, "fallback");
    expect(fallback).toEqual({ nodes: ["router"], edges: ["router-loop"], warn: true, word: "fell back" });

    // With no decision the row is the name's own: the router lit, no edge decided yet.
    expect(stageFor(ROUTE_DECIDED)).toEqual(STAGE[ROUTE_DECIDED]);
    expect(STAGE[ROUTE_DECIDED]).toEqual({ nodes: ["router"], edges: [], warn: false });

    record190("route-stage.txt", [
      "FR-124: loop.route.decided, refined by outcome and handler",
      `routed, groq: ${JSON.stringify(groq)}`,
      `routed, agent: ${JSON.stringify(agent)}`,
      `uncertain: ${JSON.stringify(uncertain)}`,
      `fallback: ${JSON.stringify(fallback)}`,
    ]);
  });

  it("lights the router uncertain or fallback in the warn treatment, with the word beside it", () => {
    prefersReducedMotion(false);
    const uncertainFrame = frameOf(
      { id: "1", event: ROUTE_DECIDED, data: '{"outcome":"uncertain","handler":"human"}' },
      "/v1/sessions/x/stream",
    );
    render(<HarnessDiagram stage={null} labels={{}} frames={[uncertainFrame]} />);
    const router = document.querySelector('[data-node][aria-label="Jev routes"]');
    expect(router?.getAttribute("data-lit")).toBe("warn");
    expect(within(screen.getByRole("group", { name: "Router" })).getByText("uncertain")).toBeTruthy();
  });

  it("lights the failed-open gate in the warn treatment with the word beside it", () => {
    render(<HarnessDiagram stage="retrieval.gate.failed_open" labels={{}} />);

    const gate = document.querySelector('[data-node][aria-label="gate"]');
    expect(gate?.getAttribute("data-lit")).toBe("warn");
    expect(within(screen.getByRole("group", { name: "Memory" })).getByText("failed open")).toBeTruthy();
    for (const pillar of ["procedural", "semantic", "episodic"]) {
      expect(document.querySelector(`[data-node][aria-label="${pillar}"]`)?.getAttribute("data-lit")).toBeNull();
    }
    expect(screen.getByRole("status").textContent).toBe("Idle");
  });

  it("lights a stage's nodes and edges, and a name that lights nothing lights nothing", () => {
    const { rerender } = render(<HarnessDiagram stage="acp.delegation.started" labels={{}} />);

    const lit = (): string[] =>
      [...document.querySelectorAll("[data-lit]")].map(
        (node) => node.getAttribute("data-node") ?? node.getAttribute("data-edge") ?? "",
      );
    const delegation: Stage = STAGE["acp.delegation.started"];
    expect(lit()).toEqual([...delegation.edges, ...delegation.nodes]);
    expect(screen.queryByText("failed open")).toBeNull();

    rerender(<HarnessDiagram stage="error.raised" labels={{}} />);
    expect(lit()).toEqual([]);

    rerender(<HarnessDiagram stage={null} labels={{}} />);
    expect(lit()).toEqual([]);
    expect(screen.getByRole("status").textContent).toBe("Idle");
  });
});

describe("acp.* and an agent route light the named entry's box (FR-124)", () => {
  it("lights the entry whose name matches loop.route.decided's own .route field, on an agent route", () => {
    prefersReducedMotion(false);
    const frame = frameOf(
      { id: "1", event: ROUTE_DECIDED, data: '{"outcome":"routed","handler":"agent","route":"bravo"}' },
      "/v1/sessions/x/stream",
    );
    render(<HarnessDiagram stage={null} labels={{}} entries={ENTRY_LEAVES} frames={[frame]} />);

    // `entries` is [alpha, bravo, charlie]: "bravo" is index 1, so its slot id is "entry1".
    expect(flashing()).toContain("entry1");
    expect(flashing()).not.toContain("entry0");
    expect(flashing()).not.toContain("entry2");
    record190("agent-route-entry.txt", [
      "FR-124: an agent route lights the router and the named entry, never the others",
      `flashing: ${flashing().join(", ")}`,
    ]);
  });

  it("lights the named entry's box on every acp.* frame, not a single delegation box", () => {
    prefersReducedMotion(false);
    for (const name of [
      "acp.delegation.started",
      "acp.permission.request",
      "acp.permission.decision",
      "acp.delegation.finished",
      "acp.delegation.cached",
      "acp.tool.called",
      "acp.tool.finished",
    ]) {
      const frame = frameOf({ id: "1", event: name, data: '{"agent":"charlie"}' }, "/v1/sessions/x/stream");
      const { unmount } = render(
        <HarnessDiagram stage={null} labels={{}} entries={ENTRY_LEAVES} frames={[frame]} />,
      );
      // "charlie" is entries index 2, so its slot id is "entry2".
      expect(flashing(), name).toContain("entry2");
      expect(flashing(), name).not.toContain("entry0");
      unmount();
    }
  });

  it("lights nothing extra for an entry beyond the six drawn slots, or one that names no entry", () => {
    prefersReducedMotion(false);
    const unknown = frameOf(
      { id: "1", event: "acp.delegation.started", data: '{"agent":"not-in-the-registry"}' },
      "/v1/sessions/x/stream",
    );
    render(<HarnessDiagram stage={null} labels={{}} entries={ENTRY_LEAVES} frames={[unknown]} />);
    expect(flashing()).toEqual(["router-delegation"]);
  });
});

describe("HarnessDiagram accessibility", () => {
  it("is an img with a name, a text status line, and boxes the keyboard reaches", () => {
    render(<HarnessDiagram stage={null} labels={{}} entries={ENTRY_LEAVES} />);

    const picture = screen.getByRole("img", { name: DIAGRAM_NAME });
    expect(picture.tagName.toLowerCase()).toBe("svg");
    // Nothing focusable sits inside the img: its children are presentational (axe nested-interactive).
    expect(picture.querySelectorAll("a, [tabindex]")).toHaveLength(0);

    const status = screen.getByRole("status");
    expect(status.tagName.toLowerCase()).toBe("p");
    expect(status.getAttribute("aria-live")).toBe("polite");
    expect(status.textContent).toBe("Idle");

    const links = [...document.querySelectorAll<SVGAElement>("a[data-node]")];
    expect(links).toHaveLength(13);
    const lines: string[] = [];
    for (const link of links) {
      expect(link.getAttribute("tabindex")).toBeNull();
      link.focus();
      expect(document.activeElement, link.getAttribute("aria-label") ?? "").toBe(link);
      lines.push(`${link.getAttribute("aria-label")}: focusable, href ${link.getAttribute("href")}`);
    }

    record("a11y.txt", [
      "FR-84, ticket 142: the diagram's accessibility",
      `role=img, name: ${picture.getAttribute("aria-label")}`,
      `status line: <p role="status" aria-live="polite">, text "${status.textContent}"`,
      `focusable descendants inside the img: 0`,
      ...lines,
    ]);
  });
});

describe("FR-124: the status line reads in human words", () => {
  it('reads "Idle" before any frame has ever arrived', () => {
    render(<HarnessDiagram stage={null} labels={{}} />);
    expect(screen.getByRole("status").textContent).toBe("Idle");
  });

  it('reads "Last step: <word>, N min ago" for the newest frame, on the dashboard clock', () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-09-28T12:00:00Z"));
    const [started] = recordedFrames();
    const { rerender } = render(<HarnessDiagram stage={null} labels={{}} frames={[started]} />);
    expect(screen.getByRole("status").textContent).toBe(`Last step: ${wordFor(started)}, 0 min ago`);

    vi.setSystemTime(new Date("2026-09-28T12:02:00Z"));
    rerender(<HarnessDiagram stage={null} labels={{}} frames={[started]} />);
    expect(screen.getByRole("status").textContent).toBe(`Last step: ${wordFor(started)}, 2 min ago`);

    record190("status-line.txt", [
      "FR-124: the status line, in human words",
      `before any frame: Idle`,
      `run.started, 0 min: Last step: ${wordFor(started)}, 0 min ago`,
      `run.started, 2 min later: Last step: ${wordFor(started)}, 2 min ago`,
    ]);
  });

  it("names the newest frame even where reduced motion runs no animation", () => {
    prefersReducedMotion(true);
    const frames = recordedFrames();
    const { rerender } = render(<HarnessDiagram stage={null} labels={{}} frames={frames.slice(0, 1)} />);
    expect(screen.getByRole("status").textContent).toBe(`Last step: ${wordFor(frames[0])}, 0 min ago`);
    rerender(<HarnessDiagram stage={null} labels={{}} frames={frames.slice(0, 2)} />);
    expect(screen.getByRole("status").textContent).toBe(`Last step: ${wordFor(frames[1])}, 0 min ago`);
  });
});

describe("FR-85: a frame lights one stage", () => {
  it("adds the highlight class to the frame's nodes and edges, and the class removes itself", () => {
    prefersReducedMotion(false);
    const [started] = recordedFrames();
    render(<HarnessDiagram stage={null} labels={{}} frames={[started]} />);

    expect(FLASH.every((name) => typeof name === "string" && name.length > 0)).toBe(true);
    expect(flashing()).toEqual(idsOf(STAGE["run.started"]));
    expect(beat()?.getAttribute("data-beat")).toBe("1");

    // The hold ends where the CSS animation ends, and nothing else ends it.
    act(() => {
      fireEvent.animationEnd(beat() as Element);
    });
    expect(flashing()).toEqual([]);
    expect(beat()).toBeNull();

    record143("class-on-frame.txt", [
      "FR-85, ticket 143: a frame adds the highlight class and it removes itself",
      `frame seq 1 ${started.name.text}: lit ${idsOf(STAGE["run.started"]).join(", ")}`,
      "after animationend on the frame's beat: lit nothing, beat gone",
    ]);
  });

  it("animates opacity and stroke only, at motion-highlight, with no timer for the highlight", () => {
    const css = readFileSync(resolve(DASHBOARD, "components/diagram/diagram.module.css"), "utf8");
    const keyframes = [...css.matchAll(/@keyframes\s+([\w-]+)\s*\{((?:[^{}]*\{[^{}]*\})*)\s*\}/g)];
    expect(keyframes.map(([, name]) => name)).toEqual(["highlight-a", "highlight-b", "beat"]);
    const animated = new Set(
      keyframes.flatMap(([, , body]) => [...body.matchAll(/([a-z-]+)\s*:/g)].map(([, property]) => property)),
    );
    expect([...animated].toSorted()).toEqual(["opacity", "stroke"]);

    const declared = [...css.matchAll(/animation:\s*([^;]+);/g)].map(([, value]) => value.trim());
    const running = declared.filter((value) => value !== "none");
    expect(running.length).toBe(3);
    for (const value of running) expect(value).toContain("var(--motion-highlight)");
    expect(css).toMatch(/@media \(prefers-reduced-motion: reduce\)\s*\{[^@]*animation: none;/);

    const tokens = readFileSync(resolve(DASHBOARD, "app/tokens.css"), "utf8");
    expect(tokens).toMatch(/--motion-highlight: 600ms;/);

    const source = readFileSync(resolve(DASHBOARD, "components/HarnessDiagram.tsx"), "utf8");
    const timers = source.match(/\b(setTimeout|setInterval|requestAnimationFrame|clearTimeout)\b/g) ?? [];
    expect(timers).toEqual([]);

    record143("motion.txt", [
      "FR-85, ticket 143: the highlight is CSS",
      `keyframes: ${keyframes.map(([, name]) => name).join(", ")}`,
      `properties animated: ${[...animated].toSorted().join(", ")}`,
      ...running.map((value) => `animation: ${value}`),
      "--motion-highlight: 600ms (app/tokens.css)",
      `timers in components/HarnessDiagram.tsx: ${timers.length}`,
    ]);
  });

  it("under reduced motion runs 0 animations: the status text changes and the class does not", () => {
    prefersReducedMotion(true);
    const frames = recordedFrames();
    const { rerender } = render(<HarnessDiagram stage={null} labels={{}} frames={[]} />);
    const status = screen.getByRole("status");
    const before = status.textContent;
    const classes = (): string[] =>
      [...document.querySelectorAll("[data-node], [data-edge]")].map((node) => node.getAttribute("class") ?? "");
    const atRest = classes();

    rerender(<HarnessDiagram stage={null} labels={{}} frames={frames.slice(0, 1)} />);
    const first = status.textContent;
    expect(classes()).toEqual(atRest);
    expect(flashing()).toEqual([]);
    expect(beat()).toBeNull();

    rerender(<HarnessDiagram stage={null} labels={{}} frames={frames.slice(0, 2)} />);
    const second = status.textContent;
    expect(classes()).toEqual(atRest);
    expect(beat()).toBeNull();

    rerender(<HarnessDiagram stage={null} labels={{}} frames={frames.slice(0, 3)} />);
    const third = status.textContent;
    expect(classes()).toEqual(atRest);
    expect(beat()).toBeNull();

    rerender(<HarnessDiagram stage={null} labels={{}} frames={frames.slice(0, 4)} />);
    const fourth = status.textContent;
    expect(classes()).toEqual(atRest);

    expect(before).toBe("Idle");
    expect(first).not.toBe(before);
    expect(first).toContain(wordFor(frames[0]));
    expect(second).not.toBe(first);
    expect(second).toContain(wordFor(frames[1]));
    expect(third).not.toBe(second);
    expect(third).toContain(wordFor(frames[2]));
    expect(fourth).not.toBe(third);
    expect(fourth).toContain(wordFor(frames[3]));

    record143("reduced-motion.txt", [
      "FR-85, ticket 143: prefers-reduced-motion: reduce",
      `status before any frame: "${before}"`,
      `status after seq 1: "${first}"`,
      `status after seq 2: "${second}"`,
      `status after seq 3: "${third}"`,
      `status after seq 4: "${fourth}"`,
      "highlight classes added: 0; beats started: 0; node and edge classes unchanged",
    ]);
  });

  it("lights a burst in seq order, each frame holding until its highlight ends", () => {
    prefersReducedMotion(false);
    const frames = recordedFrames();
    expect(frames.map((frame) => frame.seq.text)).toEqual(["1", "2", "3", "4", "5", "6", "7", "8"]);
    const { rerender } = render(<HarnessDiagram stage={null} labels={{}} frames={frames.slice(0, 3)} />);

    const seen: { seq: string; lit: string[]; flash: string }[] = [];
    const lines: string[] = [];
    for (const [index, frame] of frames.entries()) {
      // The rest of the burst arrives while the first frames are still lit: it queues behind them.
      if (index === 1) rerender(<HarnessDiagram stage={null} labels={{}} frames={frames} />);
      expect(beat()?.getAttribute("data-beat"), frame.seq.text).toBe(frame.seq.text);
      // A re-render is not a hold ending: only the animation's end moves the diagram on.
      rerender(<HarnessDiagram stage={null} labels={{}} frames={frames} />);
      expect(beat()?.getAttribute("data-beat"), frame.seq.text).toBe(frame.seq.text);
      const lit = flashing();
      const flash =
        [...document.querySelectorAll("[data-node], [data-edge]")]
          .flatMap((node) => FLASH.filter((name) => node.classList.contains(name)))
          .at(0) ?? "";
      seen.push({ seq: frame.seq.text, lit, flash });
      lines.push(`seq ${frame.seq.text} ${frame.name.text}: lit [${lit.join(", ")}]`);
      act(() => {
        fireEvent.animationEnd(beat() as Element);
      });
    }
    expect(beat()).toBeNull();
    expect(flashing()).toEqual([]);

    const skip = stageFor(GATE_DECIDED, "skip") as Stage;
    const routedGroq = stageFor(ROUTE_DECIDED, undefined, "routed_groq") as Stage;
    expect(seen.map(({ lit }) => lit)).toEqual([
      idsOf(STAGE["run.started"]),
      idsOf(routedGroq),
      idsOf(skip),
      idsOf(STAGE["loop.iteration.started"]),
      idsOf(STAGE["loop.iteration.finished"]),
      [],
      idsOf(STAGE["memory.write.committed"]),
      idsOf(STAGE["run.finished"]),
    ]);
    // Two lit frames in a row still restart the animation: the class alternates.
    for (const [index, { flash, lit }] of seen.entries()) {
      const previous = index === 0 ? undefined : seen[index - 1];
      if (previous === undefined || lit.length === 0 || previous.lit.length === 0) continue;
      expect(flash, `seq ${seen[index].seq}`).not.toBe(previous.flash);
    }

    // The status line names the newest frame - the last one shown, run.finished.
    expect(screen.getByRole("status").textContent).toBe(`Last step: ${wordFor(frames[7])}, 0 min ago`);

    record143("burst.txt", [
      "FR-85, ticket 143: a burst of the recorded stream lights in seq order",
      ...lines,
      `status after the burst: "${screen.getByRole("status").textContent}"`,
    ]);
  });

  it("names a failed-open gate in words while it is lit, and a skip lights the gate alone", () => {
    prefersReducedMotion(false);
    const [, decided] = recordedFrames();
    const gateFrame = (id: string, data: string): StreamFrame =>
      frameOf({ id, event: GATE_DECIDED, data }, decided.seq.route);
    render(
      <HarnessDiagram
        stage={null}
        labels={{}}
        frames={[
          gateFrame("2", '{"answer":"","failed_open":true,"reason":"timeout","retrieve":true}'),
          gateFrame("3", '{"answer":"no","failed_open":false,"reason":"","retrieve":false}'),
          gateFrame("4", "{}"),
        ]}
      />,
    );
    expect(flashing()).toEqual(["gate"]);
    expect(document.querySelector('[data-node][aria-label="gate"]')?.getAttribute("data-lit")).toBe("warn");
    expect(screen.getByText("failed open")).toBeTruthy();

    act(() => {
      fireEvent.animationEnd(beat() as Element);
    });
    expect(screen.queryByText("failed open")).toBeNull();
    expect(flashing()).toEqual(idsOf(stageFor(GATE_DECIDED, "skip") as Stage));

    // A decision the payload does not spell falls back to the name's own row.
    act(() => {
      fireEvent.animationEnd(beat() as Element);
    });
    expect(flashing()).toEqual(idsOf(STAGE[GATE_DECIDED]));
  });

  it("leaves a box that has never lit at rest, identically to every box before its first frame", () => {
    prefersReducedMotion(false);
    const frames = recordedFrames();
    const markup = (): string =>
      document.querySelector('[data-node][aria-label="You"]')?.outerHTML ?? "";

    const fresh = render(<HarnessDiagram stage={null} labels={{}} />);
    const atRest = markup();
    const boxes = [...document.querySelectorAll("[data-node]")];
    expect(new Set(boxes.map((box) => box.getAttribute("class"))).size).toBe(1);
    expect(boxes.every((box) => !box.hasAttribute("data-lit"))).toBe(true);
    fresh.unmount();

    // A recorded stream with no uncertain stop in it: every other stage lights and clears, You never does.
    render(<HarnessDiagram stage={null} labels={{}} frames={frames} />);
    for (const frame of frames) {
      expect(beat()?.getAttribute("data-beat"), frame.seq.text).toBe(frame.seq.text);
      expect(markup()).toBe(atRest);
      act(() => {
        fireEvent.animationEnd(beat() as Element);
      });
    }
    expect(markup()).toBe(atRest);

    record143("never-lit.txt", [
      "FR-85, ticket 143: a box with no history is at rest",
      `You before any frame and after all ${frames.length} recorded frames: ${atRest}`,
    ]);
  });
});

/**
 * The diagram on a watched stream: `lib/stream.ts`'s reader, unchanged, handing every read's
 * frames to the diagram the way the Overview will. Nothing here parses or orders a frame.
 */
function Watched({ route }: { route: string }) {
  const [frames, setFrames] = useState<readonly StreamFrame[]>([]);
  const [ended, setEnded] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    void readStream(
      route,
      undefined,
      (signal) => {
        if (signal.kind === "frames") setFrames((seen) => [...seen, ...signal.frames]);
        else setEnded(signal.kind);
      },
      controller.signal,
    );
    return () => controller.abort();
  }, [route]);
  return (
    <div data-ended={ended}>
      <HarnessDiagram stage={null} labels={{}} frames={frames} />
    </div>
  );
}

/** A 200 whose body is `chunks`, one read each, closing clean or failing after the last. */
function sse(chunks: readonly string[], ending: "close" | "drop"): Response {
  const queue = [...chunks];
  const body = new ReadableStream<Uint8Array>({
    pull(controller) {
      const chunk = queue.shift();
      if (chunk !== undefined) {
        controller.enqueue(new TextEncoder().encode(chunk));
        return;
      }
      if (ending === "close") controller.close();
      else controller.error(new Error("the connection reset"));
    },
  });
  return new Response(body, { status: 200, headers: { "content-type": "text/event-stream" } });
}

function ended(): string | null {
  return document.querySelector("[data-ended]")?.getAttribute("data-ended") ?? null;
}

describe("FR-85: the watched stream lights the diagram", () => {
  const meta = JSON.parse(readFileSync(resolve(FIXTURES, "sessions.stream.meta.json"), "utf8")) as { route: string };
  const recorded = readFileSync(resolve(FIXTURES, "sessions.stream.sse"), "utf8").replaceAll("\r\n", "\n");

  it("lights the recorded stream's frames in seq order, across reads that split a frame", async () => {
    prefersReducedMotion(false);
    // Three reads, cut mid-frame: a half-arrived frame is not a frame until the next read ends it.
    const cuts = [recorded.slice(0, 40), recorded.slice(40, 200), recorded.slice(200)];
    expect(cuts.join("")).toBe(recorded);
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(sse(cuts, "close"))));

    render(<Watched route={meta.route} />);
    await waitFor(() => expect(ended()).toBe("closed"));

    const order: string[] = [];
    for (const frame of recordedFrames()) {
      expect(beat()?.getAttribute("data-beat"), frame.seq.text).toBe(frame.seq.text);
      order.push(frame.seq.text);
      act(() => {
        fireEvent.animationEnd(beat() as Element);
      });
    }
    expect(order).toEqual(["1", "2", "3", "4", "5", "6", "7", "8"]);
    expect(beat()).toBeNull();
    await waitFor(() =>
      expect(screen.getByRole("status").textContent).toBe(`Last step: ${wordFor(recordedFrames()[7])}, 0 min ago`),
    );
  });

  it("keeps what a stream said before it dropped, and lights nothing it did not say", async () => {
    prefersReducedMotion(false);
    const [first, second] = recorded.split("\n\n");
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(sse([`${first}\n\n${second}\n\n`], "drop"))));

    render(<Watched route={meta.route} />);
    await waitFor(() => expect(ended()).toBe("dropped"));
    expect(beat()?.getAttribute("data-beat")).toBe("1");
    act(() => {
      fireEvent.animationEnd(beat() as Element);
    });
    expect(beat()?.getAttribute("data-beat")).toBe("2");
    act(() => {
      fireEvent.animationEnd(beat() as Element);
    });
    expect(beat()).toBeNull();
    expect(flashing()).toEqual([]);
  });

  it("stays at rest when the gateway refuses the subscription or never answers", async () => {
    prefersReducedMotion(false);
    const notFound = JSON.parse(
      readFileSync(resolve(FIXTURES, "notfound.sessions.stream.meta.json"), "utf8"),
    ) as { route: string; status: number; content_type: string };
    const refusal = readFileSync(resolve(FIXTURES, "notfound.sessions.stream.body"), "utf8");
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          new Response(refusal, { status: notFound.status, headers: { "content-type": notFound.content_type } }),
        ),
      ),
    );
    const refused = render(<Watched route={notFound.route} />);
    await waitFor(() => expect(ended()).toBe("refused"));
    expect(beat()).toBeNull();
    expect(flashing()).toEqual([]);
    expect(screen.getByRole("status").textContent).toBe("Idle");
    refused.unmount();

    vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new TypeError("Failed to fetch"))));
    render(<Watched route={meta.route} />);
    await waitFor(() => expect(ended()).toBe("dropped"));
    expect(beat()).toBeNull();
    expect(screen.getByRole("status").textContent).toBe("Idle");
  });
});
