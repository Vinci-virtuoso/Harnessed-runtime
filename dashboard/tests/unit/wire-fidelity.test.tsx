// @vitest-environment jsdom
/**
 * FR-45: the dashboard renders the characters the wire sent, and this test can fail.
 *
 * Three layers, each able to fail on its own (MUST rules 12, 21; decisions 31, 32):
 *   - `parseWire` keeps the body's source text, so `1.0` stays `"1.0"`;
 *   - `Value` renders `leaf.text` and nothing else, and exposes its route and path;
 *   - `tests/support/source-at.ts` compares every rendered figure with the bytes at its source
 *     path, and the same test seeds two divergences and asserts the comparison reports them.
 */
import { mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";

import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import Home from "../../app/page";
import ModelsPage from "../../app/models/page";
import ConsolePage from "../../app/console/page";
import { AppShell } from "../../components/AppShell";
import { Value } from "../../components/Value";
import { WireUnsupported, parseWire, type Leaf } from "../../lib/wire";
import { streamRoute } from "../../lib/stream";
import { figuresWithoutSource } from "../support/figures";
import { SESSION_ID } from "../support/stream-drops";
import { pageAt } from "./support/query";
import { divergences, renderedFigures, sourceCharactersByPath } from "../support/source-at";

vi.mock("next/navigation", async (importOriginal) => ({
  ...((await importOriginal()) as object),
  useSearchParams: (await import("./support/query")).useSearchParams,
  useRouter: () => ({ push: () => {}, replace: () => {} }),
}));

const DASHBOARD = resolve(import.meta.dirname, "../..");
const EVIDENCE = resolve(DASHBOARD, "../evidence/094/wire-fidelity.txt");

/**
 * A seeded response body rather than a recorded one, kept because the three divergence branches
 * below need a body whose every leaf is known by hand. The recorded set - taken from the Python
 * emitters by `tests/test_dashboard_fixtures.py` and never hand-edited (MUST rule 24) - is walked
 * in its own describe block at the foot of this file.
 */
const BODY = '{"score":1.0,"b":0.833333,"n":null,"ok":true,"s":"x","runs":[{"score":0.8}]}';
const ROUTE = "/v1/eval/runs/eval_20260917_000001";

afterEach(cleanup);

/** Where the recorded gateway responses live, and the artefact ticket 100's walk writes. */
const GATEWAY_FIXTURES = resolve(DASHBOARD, "tests/fixtures/gateway");
const EVIDENCE_100 = resolve(DASHBOARD, "../evidence/100/wire-fidelity.txt");

/** One recorded response: the name it is filed under, the route it answered, and its bytes. */
type RecordedBody = { name: string; route: string; text: string };

/**
 * Every `.body` in the recorded set, read from the directory rather than from a list kept here:
 * a case added by a re-record is walked without this file being touched, which is the whole
 * point of recording rather than authoring (decision 33).
 */
function recordedBodies(): RecordedBody[] {
  return readdirSync(GATEWAY_FIXTURES)
    .filter((entry) => entry.endsWith(".body"))
    .sort()
    .map((entry) => {
      const name = entry.slice(0, entry.length - ".body".length);
      const meta = JSON.parse(
        readFileSync(resolve(GATEWAY_FIXTURES, `${name}.meta.json`), "utf8"),
      ) as { route: string };
      return { name, route: meta.route, text: readFileSync(resolve(GATEWAY_FIXTURES, entry), "utf8") };
    });
}

/** The panel shape every screen FR will use: one fetched response, figures read off its leaves. */
function Figures({ leaves }: { leaves: readonly Leaf[] }) {
  return (
    <div>
      {leaves.map((leaf) => (
        <Value key={leaf.path} leaf={leaf} />
      ))}
    </div>
  );
}

describe("parseWire", () => {
  it("carries the body's own characters, kind, route and jq path on every leaf", () => {
    const wire = parseWire(BODY, ROUTE);

    expect(wire.at(".score")).toEqual({ kind: "number", text: "1.0", path: ".score", route: ROUTE });
    expect(wire.at(".b")).toEqual({ kind: "number", text: "0.833333", path: ".b", route: ROUTE });
    expect(wire.at(".n")).toEqual({ kind: "null", text: "null", path: ".n", route: ROUTE });
    expect(wire.at(".ok")).toEqual({ kind: "boolean", text: "true", path: ".ok", route: ROUTE });
    expect(wire.at(".s")).toEqual({ kind: "string", text: "x", path: ".s", route: ROUTE });
    expect(wire.at(".runs[0].score")).toEqual({
      kind: "number",
      text: "0.8",
      path: ".runs[0].score",
      route: ROUTE,
    });
  });

  it("keeps the raw text and its UTF-8 byte length for the provenance drawer (FR-51)", () => {
    const body = '{"s":"é"}';
    const wire = parseWire(body, ROUTE);

    expect(wire.text).toBe(body);
    // A string, because the drawer displays it and nothing in this tree calculates (decision 32).
    expect(wire.byteLength).toBe("10");
    expect(wire.route).toBe(ROUTE);
  });

  it("names the path it cannot find rather than answering with a guess", () => {
    expect(() => parseWire(BODY, ROUTE).at(".nope")).toThrow(".nope");
  });

  it("throws WireUnsupported when the source-text probe fails, and never plain-parses", async () => {
    const plain = JSON.parse.bind(JSON);
    const spy = vi.spyOn(JSON, "parse").mockImplementation((text: string, reviver?: unknown) => {
      const twoArg = reviver as ((key: string, value: unknown) => unknown) | undefined;
      // The pre-2025 parser: a reviver, but no third `context` argument to read `source` from.
      return twoArg ? plain(text, (key: string, value: unknown) => twoArg(key, value)) : plain(text);
    });

    vi.resetModules();
    const downgraded = await import("../../lib/wire");
    spy.mockClear();

    expect(() => downgraded.parseWire(BODY, ROUTE)).toThrow(downgraded.WireUnsupported);
    expect(() => downgraded.parseWire(BODY, ROUTE)).toThrow(/source text/i);
    expect(spy.mock.calls.some(([text]) => text === BODY)).toBe(false);

    vi.restoreAllMocks();
    vi.resetModules();
  });

  it("exports no `number`, so no component can be handed one (decision 32)", () => {
    const source = readFileSync(resolve(DASHBOARD, "lib/wire.ts"), "utf8");
    const withoutKindLiteral = source.replaceAll('"number"', '"<kind>"');

    expect(WireUnsupported.prototype).toBeInstanceOf(Error);
    expect(withoutKindLiteral).not.toMatch(/\bnumber\b/);
  });
});

describe("Value", () => {
  it("renders leaf.text exactly, rounding, padding and percentages nowhere", () => {
    const wire = parseWire(BODY, ROUTE);
    render(<Figures leaves={[wire.at(".score"), wire.at(".b"), wire.at(".runs[0].score")]} />);

    expect(screen.getByText("1.0").textContent).toBe("1.0");
    expect(screen.getByText("0.833333").textContent).toBe("0.833333");
    expect(screen.getByText("0.8").textContent).toBe("0.8");
    expect(screen.queryByText("1")).toBeNull();
    expect(screen.queryByText("0.83")).toBeNull();
    expect(screen.queryByText("100%")).toBeNull();
  });

  it("takes a Leaf and never a number", () => {
    const source = readFileSync(resolve(DASHBOARD, "components/Value.tsx"), "utf8");

    expect(source).toMatch(/leaf:\s*Leaf/);
    expect(source).not.toMatch(/\bnumber\b/);
  });

  it("exposes its route and path as data-route and data-path", () => {
    const wire = parseWire(BODY, ROUTE);
    render(<Figures leaves={[wire.at(".runs[0].score")]} />);

    const figure = screen.getByText("0.8");
    expect(figure.getAttribute("data-route")).toBe(ROUTE);
    expect(figure.getAttribute("data-path")).toBe(".runs[0].score");
  });

  it("names why a null figure is unknown when the panel supplies a reason", () => {
    const wire = parseWire(BODY, ROUTE);
    render(<Value leaf={wire.at(".n")} nullReason="No decision reached." />);

    expect(screen.getByText("No decision reached.").textContent).toBe("No decision reached.");
  });
});

describe("the wire-fidelity comparison", () => {
  it("passes on a faithful render and fails on each seeded divergence", () => {
    const bodies = new Map([[ROUTE, BODY]]);
    const wire = parseWire(BODY, ROUTE);
    const paths = [".score", ".b", ".n", ".ok", ".s", ".runs[0].score"];
    const log: string[] = [];

    // Branch 1: every figure straight off the wire.
    const faithful = render(<Figures leaves={paths.map((path) => wire.at(path))} />);
    const figures = renderedFigures(faithful.container);
    expect(figures).toHaveLength(paths.length);
    expect(divergences(figures, bodies)).toEqual([]);
    log.push(`faithful render: ${figures.length} figures, 0 divergences`);
    cleanup();

    // Branch 2: one figure altered by one character - `0.8` padded to `0.80`.
    const padded: Leaf = { ...wire.at(".runs[0].score"), text: "0.80" };
    const altered = render(<Figures leaves={[wire.at(".score"), padded]} />);
    const alteredFound = divergences(renderedFigures(altered.container), bodies);
    expect(alteredFound).toEqual([
      { route: ROUTE, path: ".runs[0].score", rendered: "0.80", expected: "0.8" },
    ]);
    log.push(`padded 0.8 -> 0.80: ${alteredFound.length} divergence (expected 0.8, rendered 0.80)`);
    cleanup();

    // Branch 3: a `1.0` taken through plain `JSON.parse`, which re-serializes it as `1`.
    const reserialized: Leaf = {
      kind: "number",
      text: String((JSON.parse(BODY) as { score: unknown }).score),
      path: ".score",
      route: ROUTE,
    };
    const downgraded = render(<Figures leaves={[reserialized]} />);
    const downgradedFound = divergences(renderedFigures(downgraded.container), bodies);
    expect(downgradedFound).toEqual([{ route: ROUTE, path: ".score", rendered: "1", expected: "1.0" }]);
    log.push(`plain JSON.parse of 1.0: ${downgradedFound.length} divergence (expected 1.0, rendered 1)`);

    // The oracle reads the bytes itself, so it is not wire.ts agreeing with itself.
    expect(sourceCharactersByPath(BODY).get(".score")).toBe("1.0");

    mkdirSync(dirname(EVIDENCE), { recursive: true });
    writeFileSync(EVIDENCE, `${log.join("\n")}\n`, "utf8");
  });
});

describe("every recorded gateway response (FR-45, ticket 100)", () => {
  const recorded = recordedBodies();
  const walked: string[] = [];

  it("reads a recorded set, so the walk below is not walking nothing", () => {
    expect(recorded.length).toBeGreaterThan(20);
    expect(recorded.map((body) => body.name)).toContain("eval.run.zero-case");
    expect(recorded.map((body) => body.name)).toContain("improve.cycle.undecided");
  });

  for (const body of recorded) {
    it(`renders ${body.name} with every figure equal to the characters the wire sent`, () => {
      const wire = parseWire(body.text, body.route);
      const paths = [...sourceCharactersByPath(body.text).keys()];
      const leaves = paths.map((path) => wire.at(path));

      const { container } = render(<Figures leaves={leaves} />);
      const figures = renderedFigures(container);

      expect(figures).toHaveLength(paths.length);
      expect(divergences(figures, new Map([[body.route, body.text]]))).toEqual([]);
      walked.push(`${body.name}: ${figures.length} figures off ${body.text.length} characters`);
    });
  }

  it("walked every recorded body, and enough leaves for the walk to mean something", () => {
    const figures = walked.reduce(
      (running, line) => running + Number.parseInt(line.split(": ")[1] ?? "0", 10),
      0,
    );

    expect(walked).toHaveLength(recorded.length);
    expect(figures).toBeGreaterThan(200);

    const lines = [`${recorded.length} recorded bodies, ${figures} figures, 0 divergences`, ...walked];
    mkdirSync(dirname(EVIDENCE_100), { recursive: true });
    writeFileSync(EVIDENCE_100, `${lines.join("\n")}\n`, "utf8");
  });
});

/**
 * FR-123 (ticket 192): every figure v5 added - the console's route lines, captions and cost
 * lines, the Models cards, the diagram's labels, the Overview's activity rows and Spent buckets -
 * is walked on its own page, over the recorded bodies. Four claims, each able to fail:
 *   - every figure names a source label and a field (its tooltip, and `data-route` + `data-path`);
 *   - `data-route` and `data-path` are never visible, never a `title`, never an accessible name;
 *   - 0 figures' exact text differs from the wire (a human rendering is allowed only where the
 *     tooltip carries the exact wire text, `D-4`);
 *   - the census is not empty.
 */
describe("every page's figures (FR-123, ticket 192)", () => {
  const EVIDENCE_192 = resolve(DASHBOARD, "../evidence/192/wire-fidelity.txt");
  const FROZEN = new Date("2026-09-23T09:15:00");
  const census: string[] = [];

  function body(name: string): string {
    return readFileSync(resolve(GATEWAY_FIXTURES, `${name}.body`), "utf8");
  }

  const SESSION_MESSAGES = `/v1/sessions/${SESSION_ID}/messages`;
  const SESSION_DECISIONS = `/v1/sessions/${SESSION_ID}/decisions`;
  const SESSION_SPEND = `/v1/spend?session=${encodeURIComponent(SESSION_ID)}`;

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
    ["/v1/providers", body("providers")],
    ["/v1/prices", body("prices")],
    ["/v1/agents", body("agents")],
    ["/v1/router", body("router")],
    [SESSION_MESSAGES, body("sessions.messages")],
    [SESSION_DECISIONS, body("sessions.decisions")],
    [SESSION_SPEND, body("spend.session")],
  ]);

  function gateway(input: RequestInfo | URL): Promise<Response> {
    const url = String(input);
    if (url.endsWith("/stream")) {
      const opened = new ReadableStream<Uint8Array>({ start() {} });
      return Promise.resolve(new Response(opened, { status: 200, headers: { "content-type": "text/event-stream" } }));
    }
    const found = BODIES.get(url);
    const text = found ?? body("notfound.route");
    return Promise.resolve(
      new Response(text, { status: found === undefined ? 404 : 200, headers: { "content-type": "application/json" } }),
    );
  }

  async function flushed(): Promise<void> {
    for (let pass = 0; pass < 8; pass += 1) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
    }
  }

  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["Date", "setTimeout", "clearTimeout", "setInterval", "clearInterval"] });
    vi.setSystemTime(FROZEN);
    vi.stubGlobal("matchMedia", (query: string) => ({ matches: true, media: query }));
    vi.stubGlobal("fetch", vi.fn(gateway));
    // The streamed-route constant is only read to keep the import honest about what is held open.
    void streamRoute(SESSION_ID);
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  /** One page's whole census; a failure names the figure, its route and its path. */
  function walk(name: string, container: HTMLElement): void {
    const figures = renderedFigures(container);
    expect(figures.length, `${name}: figures found`).toBeGreaterThan(0);
    expect(figuresWithoutSource(container), `${name}: figures without a source`).toEqual([]);

    // A human rendering may differ from the wire only where its tooltip holds the exact wire text.
    const unequal = divergences(figures, BODIES).filter((found) => {
      const node = container.querySelector(
        `[data-route="${CSS.escape(found.route)}"][data-path="${CSS.escape(found.path)}"]`,
      );
      const title = node?.getAttribute("title") ?? "";
      return found.expected === undefined || !title.endsWith(`, ${found.expected}`);
    });
    expect(unequal, `${name}: figures that differ from the wire`).toEqual([]);

    for (const node of container.querySelectorAll("[data-route],[data-path]")) {
      const route = node.getAttribute("data-route") ?? "";
      const path = node.getAttribute("data-path") ?? "";
      const named = [node.getAttribute("title"), node.getAttribute("aria-label")].join(" ");
      expect(named.includes(route) && route !== "", `${name}: ${route} in a title or name`).toBe(false);
      expect(named.includes(path) && path !== "", `${name}: ${path} in a title or name`).toBe(false);
      expect(node.textContent, `${name}: ${route} ${path} shown as text`).not.toBe(route);
      expect(node.textContent, `${name}: ${route} ${path} shown as text`).not.toBe(path);
    }
    for (const labelled of container.querySelectorAll("[aria-labelledby]")) {
      const target = document.getElementById(labelled.getAttribute("aria-labelledby") ?? "");
      expect(target?.hasAttribute("data-route") ?? false, `${name}: a figure names an element`).toBe(false);
    }
    census.push(`${name}: ${figures.length} figures, 0 without a source, 0 differ from the wire`);
  }

  it("Overview (activity rows, Spent buckets, diagram labels)", async () => {
    const { container } = render(<AppShell><Home /></AppShell>);
    await flushed();
    walk("overview", container);
  });

  it("Models (provider, price, router and agent cards)", async () => {
    const { container } = render(<AppShell>{ModelsPage()}</AppShell>);
    await flushed();
    walk("models", container);
  });

  it("Console (route lines, captions, cost lines)", async () => {
    const { container } = render(<AppShell>{pageAt(ConsolePage, { session: SESSION_ID })}</AppShell>);
    await flushed();
    walk("console", container);
  });

  it("writes the census", () => {
    expect(census).toHaveLength(3);
    mkdirSync(dirname(EVIDENCE_192), { recursive: true });
    writeFileSync(EVIDENCE_192, `${census.join("\n")}\n`, "utf8");
  });
});
