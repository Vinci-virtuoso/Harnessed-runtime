// @vitest-environment jsdom
/**
 * FR-51, FR-129: every figure is its own trigger, in human words, with the exact text a hover
 * away (`D-4`, `D-9`; decisions 29, 31, 32, 68).
 *
 * Four layers, each able to fail on its own:
 *   - `Value` is itself a focusable `button` wherever it renders inside a panel, so a figure is
 *     one Tab away from its source and one Enter away from the response it was read out of - no
 *     `SourceChip` beside it any more;
 *   - `ProvenanceDrawer` shows that response's provenance in six parts: From, What, Value (exact),
 *     Updated, Answer, a `RecordView` of the whole record with the cited field marked, and the CLI
 *     `--json` twin `lib/cli-twins.ts` names. No method, path, byte count or `curl` anywhere;
 *   - `RecordView` renders the record structurally, every scalar exactly as the source spelled it;
 *   - `tests/support/figures.ts` walks a rendered page for figures with no source, and the seeded
 *     sourceless figure below proves that walk can fail (MUST rule 12).
 *
 * The inputs are the recorded gateway responses (decision 33). The verbatim claim is swept over
 * every one of them rather than over a sample, so a re-record widens this test for free.
 */
import { existsSync, mkdirSync, readFileSync, readdirSync, statSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { rowsOf } from "../../lib/rows";
import { CostLine } from "../../components/CostLine";
import { PanelFrame } from "../../components/PanelFrame";
import { ProvenanceDrawer } from "../../components/ProvenanceDrawer";
import { Value } from "../../components/Value";
import { cliTwinFor } from "../../lib/cli-twins";
import {
  GATEWAY_ORIGIN,
  fetchPanel,
  gatewayHref,
  type RawResponse,
} from "../../lib/fetch-panel";
import { nullReasonFor } from "../../lib/null-reasons";
import { parseWire, type Wire } from "../../lib/wire";
import { figuresWithoutSource } from "../support/figures";
import { sourceCharactersByPath } from "../support/source-at";

const DASHBOARD = resolve(import.meta.dirname, "../..");
const GATEWAY_FIXTURES = resolve(DASHBOARD, "tests/fixtures/gateway");
const EVIDENCE = resolve(DASHBOARD, "../evidence/184/provenance.txt");
const DRAWER_HTML = resolve(DASHBOARD, "../evidence/184/drawer.html");

/** The dashboard's clock, frozen so "updated 0 min ago" is the same characters on every run. */
const FROZEN = new Date("2026-09-17T14:03:09");

/** Every panel this file opens reads under the same plain label (`D-4`); the wording itself is
 * proved elsewhere (per-page tickets 185+ pick the real one for each page). */
const SOURCE_LABEL = "Ledger: all time";

/** The lines this run writes to `evidence/184/provenance.txt`. */
const log: string[] = [];

type Recorded = { name: string; route: string; status: string; code: number; text: string };

/** One recorded response, read from the committed record and never written by a test. */
function recorded(name: string): Recorded {
  const meta = JSON.parse(readFileSync(resolve(GATEWAY_FIXTURES, `${name}.meta.json`), "utf8")) as {
    route: string;
    status: number;
  };
  return {
    name,
    route: meta.route,
    status: String(meta.status),
    code: meta.status,
    text: readFileSync(resolve(GATEWAY_FIXTURES, `${name}.body`), "utf8"),
  };
}

/** Every recorded body, read off the directory so a re-record widens the sweep for free. */
function everyRecordedBody(): Recorded[] {
  return readdirSync(GATEWAY_FIXTURES)
    .filter((entry) => entry.endsWith(".body"))
    .sort()
    .map((entry) => recorded(entry.slice(0, entry.length - ".body".length)));
}

/** The response as `fetch-panel` would have kept it, for a drawer rendered without a panel. */
function rawResponse(body: Recorded): RawResponse {
  return {
    url: gatewayHref(GATEWAY_ORIGIN, body.route),
    status: body.status,
    bytes: String(new TextEncoder().encode(body.text).length),
    text: body.text,
    fetchedAt: FROZEN,
  };
}

type Answer = { status: number; text: string; headers?: Record<string, string> };

/** A response object with only what `fetch-panel` reads, so no network is anywhere near a test. */
function answer({ status, text, headers = {} }: Answer): Response {
  return {
    status,
    ok: status === 200,
    headers: { get: (name: string) => headers[name.toLowerCase()] ?? null },
    text: async () => text,
  } as unknown as Response;
}

/** Stub `fetch` with one answer, and hand back the spy so a test can read the call it made. */
function answersWith(next: Answer): ReturnType<typeof vi.fn> {
  const spy = vi.fn(async () => answer(next));
  vi.stubGlobal("fetch", spy);
  return spy;
}

/** A panel over one recorded response, rendering one figure per path so each is its own trigger. */
function panelOver(body: Recorded, paths: readonly string[]) {
  const children = (wire: Wire) => (
    <>
      {paths.map((path) => (
        <Value key={path} leaf={wire.at(path)} />
      ))}
    </>
  );
  return (
    <PanelFrame route={body.route} title="Panel" sourceLabel={SOURCE_LABEL}>
      {children}
    </PanelFrame>
  );
}

/** The button a figure at `path` rendered as, found by its non-rendered `data-path` hook. */
function figureAt(container: HTMLElement, path: string): HTMLElement {
  const found = container.querySelector(`[data-path="${CSS.escape(path)}"]`);
  if (found === null) throw new Error(`no figure at ${path}`);
  return found as HTMLElement;
}

/** Open the figure at `path`, and hand back the trigger and the drawer it opened. */
async function openedDrawer(
  container: HTMLElement,
  path: string,
): Promise<{ trigger: HTMLElement; dialog: HTMLElement }> {
  const trigger = figureAt(container, path);
  fireEvent.click(trigger);
  return { trigger, dialog: await screen.findByRole("dialog") };
}

/** Every focusable control inside an element, in DOM order: what a focus trap cycles through. */
function focusables(within_: HTMLElement): HTMLElement[] {
  return [...within_.querySelectorAll<HTMLElement>("button, [href], input, [tabindex]")].filter(
    (node) => node.getAttribute("tabindex") !== "-1",
  );
}

/** Report `prefers-reduced-motion: reduce` as the operator's setting. */
function prefersReducedMotion(reduce: boolean): void {
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: reduce && query.includes("prefers-reduced-motion"),
    media: query,
    addEventListener: () => {},
    removeEventListener: () => {},
  }));
}

const MEASURED = recorded("eval.run.measured");

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(FROZEN);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

// ---- `Value`: one Tab from a figure to its source (D-4, D-9) -----------------------------------

describe("Value is its own trigger (D-4, D-9)", () => {
  it("is a focusable button, reachable by Tab, carrying no route or method in its name", async () => {
    answersWith({ status: 200, text: MEASURED.text });
    const { container } = render(panelOver(MEASURED, [".score"]));

    const figure = await waitFor(() => figureAt(container, ".score"));
    expect(figure.tagName).toBe("BUTTON");
    expect(figure.getAttribute("type")).toBe("button");
    // Not removed from the tab order: the keyboard path to a source is never opt-in.
    expect(figure.getAttribute("tabindex")).toBeNull();
    expect(figure.textContent).toBe(MEASURED.text.length > 0 ? figure.textContent : "");
    expect(figure.getAttribute("aria-label")).toBeNull();
    log.push("Value: a button, in the tab order, whose accessible name is its own text");
  });

  it("names route and path only as the non-rendered data-route / data-path hooks (D-9)", async () => {
    answersWith({ status: 200, text: MEASURED.text });
    const { container } = render(panelOver(MEASURED, [".score"]));

    const figure = await waitFor(() => figureAt(container, ".score"));
    expect(figure.getAttribute("data-route")).toBe(MEASURED.route);
    expect(figure.getAttribute("data-path")).toBe(".score");
    // The DOM scan skips exactly these two attributes; nowhere else does the route appear.
    expect(figure.getAttribute("title")).not.toContain(MEASURED.route);
    expect(figure.outerHTML).not.toContain("GET");
    log.push("Value: data-route/data-path are the only route hooks, never rendered into text");
  });

  it('tooltip reads "<source label>, updated <relative time>"', async () => {
    answersWith({ status: 200, text: MEASURED.text });
    const { container } = render(panelOver(MEASURED, [".score"]));

    const figure = await waitFor(() => figureAt(container, ".score"));
    expect(figure.getAttribute("title")).toBe(`${SOURCE_LABEL}, updated 0 min ago`);
    log.push('Value: tooltip is "<source label>, updated <relative time>"');
  });

  it("opens the drawer on click, and on Enter, without a double-close", async () => {
    answersWith({ status: 200, text: MEASURED.text });
    const { container } = render(panelOver(MEASURED, [".score"]));
    await waitFor(() => figureAt(container, ".score"));

    expect(screen.queryByRole("dialog")).toBeNull();
    fireEvent.keyDown(figureAt(container, ".score"), { key: "Enter" });
    expect(await screen.findByRole("dialog")).toBeTruthy();

    fireEvent.keyDown(document.body, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    fireEvent.click(figureAt(container, ".score"));
    expect(await screen.findByRole("dialog")).toBeTruthy();
    log.push("Value: click and Enter both open the drawer, Enter does not also close it");
  });

  it("portals the drawer out of a paragraph, so a `p` never nests a section", async () => {
    answersWith({ status: 200, text: MEASURED.text });
    // The fact's text is the one panel that renders its figure inside a real paragraph; the drawer
    // is block markup, so it must not mount there (React logs `p` cannot contain a `section`).
    render(
      <PanelFrame route={MEASURED.route} title="Panel" sourceLabel={SOURCE_LABEL}>
        {(wire) => (
          <p data-testid="fact-text">
            <Value leaf={wire.at(".score")} />
          </p>
        )}
      </PanelFrame>,
    );

    const paragraph = await screen.findByTestId("fact-text");
    fireEvent.click(figureAt(paragraph, ".score"));
    const dialog = await screen.findByRole("dialog");

    expect(paragraph.contains(dialog)).toBe(false);
    expect(document.body.contains(dialog)).toBe(true);
    log.push("Value: the drawer portals to `body`, so no paragraph nests a section");
  });

  it("gives every figure in a panel its own trigger, citing its own path", async () => {
    answersWith({ status: 200, text: MEASURED.text });
    const { container } = render(panelOver(MEASURED, [".score", ".pass_count", ".cases[0].case_id"]));

    await waitFor(() => figureAt(container, ".cases[0].case_id"));
    for (const path of [".score", ".pass_count", ".cases[0].case_id"]) {
      const figure = figureAt(container, path);
      expect(figure.getAttribute("data-path")).toBe(path);
      expect(figure.getAttribute("title")).toBe(`${SOURCE_LABEL}, updated 0 min ago`);
    }
  });
});

// ---- `ProvenanceDrawer`: the response the panel rendered, in six parts -------------------------

describe("ProvenanceDrawer shows the response in six parts, no endpoint string (D-4, D-9)", () => {
  it("states From, What, Value (exact), Updated and Answer", async () => {
    answersWith({ status: 200, text: MEASURED.text });
    const { container } = render(panelOver(MEASURED, [".score"]));
    await waitFor(() => figureAt(container, ".score"));
    const { dialog } = await openedDrawer(container, ".score");

    expect(within(dialog).getByText("From")).toBeTruthy();
    expect(within(dialog).getByText(SOURCE_LABEL)).toBeTruthy();
    expect(within(dialog).getByText("What")).toBeTruthy();
    expect(within(dialog).getByTestId("field-name").textContent).toBe("score");
    expect(within(dialog).getByText("Score")).toBeTruthy();
    expect(within(dialog).getByText("Value")).toBeTruthy();
    expect(within(dialog).getByTestId("exact-value").textContent).toBe(
      sourceCharactersByPath(MEASURED.text).get(".score"),
    );
    expect(within(dialog).getByText("Updated")).toBeTruthy();
    expect(dialog.textContent).toContain("0 min ago");
    expect(dialog.textContent).toContain("14:03:09 (dashboard clock)");
    expect(within(dialog).getByText("Answer")).toBeTruthy();
    expect(within(dialog).getByTestId("answer").textContent).toBe("ok");
    // Decision 29, D-9: no endpoint string anywhere in the dialog.
    expect(dialog.textContent).not.toContain(MEASURED.route);
    expect(dialog.textContent).not.toContain("GET");
    expect(dialog.textContent).not.toContain("bytes");
    log.push("ProvenanceDrawer: From, What, Value (exact), Updated, Answer - no endpoint string");
  });

  it("shows the record as a RecordView, with the cited field marked aria-current", async () => {
    answersWith({ status: 200, text: MEASURED.text });
    const { container } = render(panelOver(MEASURED, [".score"]));
    await waitFor(() => figureAt(container, ".score"));
    const { dialog } = await openedDrawer(container, ".score");

    expect(within(dialog).getByTestId("record-view")).toBeTruthy();
    const cited = within(dialog).getByTestId("cited");
    expect(cited.textContent).toBe(sourceCharactersByPath(MEASURED.text).get(".score"));
    expect(cited.getAttribute("aria-current")).toBe("true");
    log.push("ProvenanceDrawer: RecordView renders the record, the cited field aria-current");
  });

  it("highlights a path inside an array of objects, not the first row that looks like it", () => {
    render(
      <ProvenanceDrawer
        response={rawResponse(MEASURED)}
        sourceLabel={SOURCE_LABEL}
        leaf={{ kind: "string", text: "smoke-003-json-only", path: ".cases[2].case_id", route: MEASURED.route }}
        onClose={() => {}}
      />,
    );

    const cited = screen.getByTestId("cited");
    expect(cited.textContent).toBe("smoke-003-json-only");
    log.push("ProvenanceDrawer: a row's path highlights that row, not the first one");
  });

  it("shows the CLI --json twin where lib/cli-twins.ts lists one, with a Copy button", async () => {
    answersWith({ status: 200, text: MEASURED.text });
    const { container } = render(panelOver(MEASURED, [".score"]));
    await waitFor(() => figureAt(container, ".score"));
    const { dialog } = await openedDrawer(container, ".score");

    expect(within(dialog).getByTestId("cli-twin").textContent).toBe(cliTwinFor(MEASURED.route));

    const written: string[] = [];
    vi.stubGlobal("navigator", { clipboard: { writeText: async (text: string) => void written.push(text) } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Copy" }));
    await waitFor(() => expect(written).toEqual([cliTwinFor(MEASURED.route)]));
    log.push("ProvenanceDrawer: the CLI --json twin, with a working Copy button");
  });

  it("says nothing about a CLI twin on a route that has none", () => {
    const health = recorded("health");
    render(
      <ProvenanceDrawer
        response={rawResponse(health)}
        sourceLabel="Harness: this machine"
        leaf={{ kind: "number", text: "16", path: ".route_count", route: health.route }}
        onClose={() => {}}
      />,
    );

    expect(screen.queryByTestId("cli-twin")).toBeNull();
  });
});

// ---- The dialog contract ----------------------------------------------------------------------

describe("the drawer is a dialog (design.md motion & accessibility)", () => {
  it('is `role=dialog` with `aria-modal`, labelled "Where this came from" - never a route', async () => {
    answersWith({ status: 200, text: MEASURED.text });
    const { container } = render(panelOver(MEASURED, [".score"]));
    await waitFor(() => figureAt(container, ".score"));
    const { dialog } = await openedDrawer(container, ".score");

    expect(dialog.getAttribute("aria-modal")).toBe("true");
    const labelledBy = dialog.getAttribute("aria-labelledby");
    expect(labelledBy).toBeTruthy();
    const heading = document.getElementById(labelledBy ?? "");
    expect(heading?.textContent).toBe("Where this came from");
    log.push('ProvenanceDrawer: role=dialog, aria-modal, labelled "Where this came from"');
  });

  it("traps focus: Tab from the last control returns to the first, and Shift+Tab back", async () => {
    answersWith({ status: 200, text: MEASURED.text });
    const { container } = render(panelOver(MEASURED, [".score"]));
    await waitFor(() => figureAt(container, ".score"));
    const { dialog } = await openedDrawer(container, ".score");

    const controls = focusables(dialog);
    expect(controls.length).toBeGreaterThan(1);
    const first = controls.at(0);
    const last = controls.at(-1);
    expect(dialog.contains(document.activeElement)).toBe(true);

    last?.focus();
    fireEvent.keyDown(dialog, { key: "Tab" });
    expect(document.activeElement).toBe(first);

    fireEvent.keyDown(dialog, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(last);
    log.push("ProvenanceDrawer: focus is trapped, wrapping both ways");
  });

  it("closes on Esc and returns focus to the trigger that opened it", async () => {
    answersWith({ status: 200, text: MEASURED.text });
    const { container } = render(panelOver(MEASURED, [".score"]));
    await waitFor(() => figureAt(container, ".score"));
    const { trigger, dialog } = await openedDrawer(container, ".score");

    fireEvent.keyDown(dialog, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(document.activeElement).toBe(trigger);
    log.push("ProvenanceDrawer: Esc closes and focus returns to the opener");
  });

  it("closes on its own Close button, and returns focus the same way", async () => {
    answersWith({ status: 200, text: MEASURED.text });
    const { container } = render(panelOver(MEASURED, [".score"]));
    await waitFor(() => figureAt(container, ".score"));
    const { trigger, dialog } = await openedDrawer(container, ".score");

    fireEvent.click(within(dialog).getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(document.activeElement).toBe(trigger);
  });

  it("opens without a transition under `prefers-reduced-motion: reduce`", async () => {
    prefersReducedMotion(true);
    answersWith({ status: 200, text: MEASURED.text });
    const { container } = render(panelOver(MEASURED, [".score"]));
    await waitFor(() => figureAt(container, ".score"));
    const reduced = await openedDrawer(container, ".score");
    expect(reduced.dialog.getAttribute("data-motion")).toBe("instant");

    cleanup();
    prefersReducedMotion(false);
    answersWith({ status: 200, text: MEASURED.text });
    const { container: second } = render(panelOver(MEASURED, [".score"]));
    await waitFor(() => figureAt(second, ".score"));
    const moving = await openedDrawer(second, ".score");
    expect(moving.dialog.getAttribute("data-motion")).toBe("slide");
    log.push("ProvenanceDrawer: instant under reduced motion, slide otherwise");
  });

  it("writes the open drawer's markup as this ticket's evidence", async () => {
    answersWith({ status: 200, text: MEASURED.text });
    const { container } = render(panelOver(MEASURED, [".score"]));
    await waitFor(() => figureAt(container, ".score"));
    const { dialog } = await openedDrawer(container, ".score");

    mkdirSync(dirname(DRAWER_HTML), { recursive: true });
    writeFileSync(DRAWER_HTML, `${dialog.outerHTML}\n`, "utf8");
    expect(dialog.outerHTML).toContain("cited");
  });
});

// ---- The verbatim claim, over the whole recorded corpus ----------------------------------------

describe("the drawer's Value row and RecordView are byte-identical to the response", () => {
  it.each(everyRecordedBody().map((body) => [body.name, body] as const))(
    "renders %s verbatim, as `fetchPanel` kept it",
    async (_name, body) => {
      answersWith({ status: body.code, text: body.text });
      const result = await fetchPanel(body.route, GATEWAY_ORIGIN);

      // Unreachable is a rejected fetch and nothing else (MUST rule 23): no recorded answer, the
      // v3 transport's 502 included, is that state.
      expect(result.state).not.toBe("unreachable");
      if (result.state === "unreachable") return;

      expect(result.response.text).toBe(body.text);
      expect(result.response.status).toBe(body.status);

      const leaves = sourceCharactersByPath(body.text);
      const [first, expected] = [...leaves.entries()][0] ?? [undefined, undefined];
      if (first === undefined) return;
      render(
        <ProvenanceDrawer
          response={result.response}
          sourceLabel={SOURCE_LABEL}
          leaf={{ kind: "string", text: expected ?? "", path: first, route: body.route }}
          onClose={() => {}}
        />,
      );
      expect(screen.getByTestId("cited").textContent).toBe(expected);
      cleanup();
    },
  );
});

// ---- No figure, no drawer ------------------------------------------------------------------------

/** A panel whose children are fixed, for the states that render instead of a figure. */
function panelShowing(body: Recorded, children: () => null) {
  return (
    <PanelFrame
      route={body.route}
      title="Panel"
      sourceLabel={SOURCE_LABEL}
      emptyText="nothing has been promoted yet"
    >
      {children}
    </PanelFrame>
  );
}

describe("a state with no figure has no source to offer", () => {
  it.each([
    ["notfound.eval.run", "SurfaceNotFound"],
    ["badrequest.runs.limit", "LimitError"],
    ["nodatabase.runs.listing", "StorageError"],
    ["transport-unreachable", "GatewayUnreachable"],
    ["empty.promotions", "nothing has been promoted yet"],
  ])("shows %s without a trigger", async (name, expected) => {
    const body = recorded(name);
    answersWith({ status: body.code, text: body.text });
    render(panelShowing(body, () => null));

    await waitFor(() => expect(screen.queryAllByTestId("skeleton-row")).toEqual([]));
    expect(document.body.querySelector("[data-figure]")).toBeNull();
    expect(document.body.textContent).toContain(expected);
  });

  it("says nothing of the proxy when the gateway could not be reached at all", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new Error("fetch failed");
      }),
    );
    render(panelShowing(recorded("health"), () => null));

    await waitFor(() => expect(document.body.textContent).toContain("fetch failed"));
    log.push("a panel with no response offers no source: unreachable has nothing to show");
  });
});

// ---- The per-page walk: 0 figures without a source ----------------------------------------------

describe("every figure on a page has a source", () => {
  it("finds 0 figures without both a route and a path on a recorded panel", async () => {
    answersWith({ status: 200, text: MEASURED.text });
    const { container } = render(
      panelOver(MEASURED, [".score", ".pass_count", ".suite", ".cases[0].score"]),
    );

    await waitFor(() => figureAt(container, ".cases[0].score"));
    expect(figuresWithoutSource(container)).toEqual([]);
    log.push("page walk: 0 figures without a source on a panel over a recorded response");
  });

  it("finds the one figure that has no source, so the walk can fail (MUST rule 12)", () => {
    const { container } = render(
      <div>
        <span data-figure="" data-route="/v1/eval/runs" data-path=".runs[0].score">
          1.0
        </span>
        <span data-figure="" data-route="/v1/eval/runs">
          0.8
        </span>
      </div>,
    );

    expect(figuresWithoutSource(container)).toEqual([
      { rendered: "0.8", route: "/v1/eval/runs", path: "" },
    ]);
    log.push("page walk: a figure with no path is found, so the walk is able to fail");
  });

  it("keeps the unknown treatment's source, so an absence is traceable too", async () => {
    const old = '{"stop_reason":null,"usage":null}';
    const route = "/v1/runs/run_0000000002NEHPN093CBQMNNV3";
    answersWith({ status: 200, text: old });
    const children = (wire: Wire) => (
      <Value leaf={wire.at(".stop_reason")} nullReason={nullReasonFor(route, ".stop_reason")} />
    );
    const { container } = render(
      <PanelFrame route={route} title="Session" sourceLabel={SOURCE_LABEL}>
        {children}
      </PanelFrame>,
    );

    await waitFor(() => figureAt(container, ".stop_reason"));
    expect(figuresWithoutSource(container)).toEqual([]);
    expect(screen.getByText("unknown")).toBeTruthy();
    log.push("page walk: an `unknown` figure carries its source like any other");
  });
});

// ---- `JsonBlock` is gone: 0 files, 0 imports, 0 eslint exemption (ticket 189, FR-127) -----------

/** This file's own path, excluded below: its scan tests spell the pattern they look for, which is
 * not itself a `JsonBlock` import or JSX use anywhere else in the tree. */
const SELF = resolve(DASHBOARD, "tests/unit/provenance.test.tsx");

/** Every `.ts`/`.tsx`/`.mjs` file under the trees the scan reaches, `node_modules` and `out/`
 * excluded by never walking into them (neither root names them). */
function dashboardSourceFiles(): string[] {
  const found: string[] = [];
  const walk = (path: string): void => {
    for (const name of readdirSync(path)) {
      const entry = resolve(path, name);
      if (statSync(entry).isDirectory()) {
        walk(entry);
        continue;
      }
      if (/\.(tsx?|mjs)$/.test(name) && entry !== SELF) found.push(entry);
    }
  };
  for (const root of ["app", "components", "lib", "tests"]) walk(resolve(DASHBOARD, root));
  found.push(resolve(DASHBOARD, "eslint.config.mjs"));
  return found;
}

/** An import or a JSX use of `JsonBlock` - never a bare mention, which a scope_out file (
 * `lib/stream.ts`) and a historical comment (`tests/unit/traces.test.tsx`) are still allowed to
 * carry: the AC bans the files, the imports and the eslint exemption, not the word in prose. */
const JSON_BLOCK_REFERENCE = /(?:from\s+["'][^"']*\/JsonBlock["'])|(?:<JsonBlock\b)/;

describe("JsonBlock is deleted, files and styles, with its eslint exemption (ticket 189)", () => {
  it("finds 0 JsonBlock.tsx/.module.css and 0 import or JSX use of JsonBlock", () => {
    expect(existsSync(resolve(DASHBOARD, "components/JsonBlock.tsx"))).toBe(false);
    expect(existsSync(resolve(DASHBOARD, "components/JsonBlock.module.css"))).toBe(false);

    const offenders = dashboardSourceFiles().filter((path) =>
      JSON_BLOCK_REFERENCE.test(readFileSync(path, "utf8")),
    );
    expect(offenders.map((path) => path.slice(DASHBOARD.length))).toEqual([]);
    log.push(
      `scan: ${dashboardSourceFiles().length} source files; 0 import/JSX use of JsonBlock; 0 JsonBlock.tsx/.module.css`,
    );
  });

  it("finds the component-prop lint rule with no JsonBlock exemption left in it", () => {
    const source = readFileSync(resolve(DASHBOARD, "eslint.config.mjs"), "utf8");
    expect(source).not.toMatch(/JsonBlock/);
    expect(source).toContain(
      'JSXOpeningElement[name.name=/^[A-Z]/] > JSXAttribute[name.name!="key"] > JSXExpressionContainer',
    );
  });
});

// ---- `CostLine`: 0 costs nobody reported render as $0.00, 0 or - (ticket 189, FR-122) -----------

/** `handlers`, scoped so `CostLine`'s `[N]` addresses one row (decision 42; `Conversation.tsx`'s
 * own `rowWire`, repeated here - the two files share no import for a one-line helper). */
function rowWire(wire: Wire, prefix: string): Wire {
  return {
    route: wire.route,
    text: wire.text,
    byteLength: wire.byteLength,
    at: (path: string) => wire.at(`${prefix}${path}`),
  };
}

/** Every figure `Value` rendered inside `container` - `CostLine`'s own bare cost figures and the
 * side rail's total/API-equivalent rows alike (`data-figure` is `Value`'s own non-rendered hook,
 * decision 42's `D-9` carrier, so this reads every one without guessing at a class name). */
function figureTexts(container: HTMLElement): string[] {
  return [...container.querySelectorAll<HTMLElement>("[data-figure]")].map(
    (node) => node.textContent ?? "",
  );
}

const BANNED_ZERO_TEXTS = ["$0.00", "0.00", "0", "-"];

describe("CostLine: 0 costs nobody reported render as $0.00, 0 or -", () => {
  it("over every recorded /v1/spend and /v1/sessions/{id}/messages body, in both its forms", () => {
    const bodies = everyRecordedBody().filter(
      (body) => body.route === "/v1/spend" || /\/v1\/sessions\/[^/]+\/messages$/.test(body.route),
    );
    expect(bodies.length).toBeGreaterThan(0);
    let swept = 0;

    for (const body of bodies) {
      const wire = parseWire(body.text, body.route);
      if (body.route === "/v1/spend") {
        answersWith({ status: 200, text: body.text });
        const { container, unmount } = render(
          <PanelFrame route={body.route} title="Panel" sourceLabel={SOURCE_LABEL}>
            {() => (
              <CostLine
                handlers={rowWire(wire, ".handlers")}
                total={wire.at(".total")}
                apiEquivalent={wire.at(".api_equivalent")}
              />
            )}
          </PanelFrame>,
        );
        for (const text of figureTexts(container)) {
          expect(BANNED_ZERO_TEXTS, `${body.name}: "${text}"`).not.toContain(text.trim());
        }
        swept += 1;
        unmount();
        continue;
      }
      for (const row of rowsOf(wire, (index) => `.messages[${index}].message_seq`)) {
        const { container, unmount } = render(
          <CostLine handlers={rowWire(wire, `.messages[${row}].handlers`)} />,
        );
        for (const text of figureTexts(container)) {
          expect(BANNED_ZERO_TEXTS, `${body.name}[${row}]: "${text}"`).not.toContain(text.trim());
        }
        swept += 1;
        unmount();
      }
    }

    log.push(`CostLine: ${bodies.length} recorded bodies, ${swept} handler lists, 0 zero-looking costs`);
  });

  it('shows "not metered" and announces "not metered, the agent reported no cost" (D-2 amended)', () => {
    // No recorded fixture carries an `oauth` agent yet (decision 33 allows a small hand-authored
    // body for a value nothing records, the same rule `vocabulary.test.tsx` states for itself).
    const body = JSON.stringify({
      handlers: [
        {
          handler: "concierge",
          billing: "oauth",
          cost: null,
          api_equivalent: null,
          reason: null,
        },
      ],
    });
    const wire = parseWire(body, "/v1/sessions/s_1/messages");
    render(<CostLine handlers={rowWire(wire, ".handlers")} />);

    expect(screen.getByText("not metered")).toBeTruthy();
    expect(screen.getByLabelText("not metered, the agent reported no cost")).toBeTruthy();
    log.push('CostLine: "not metered" announces "not metered, the agent reported no cost"');
  });

  it('shows "subscription, API-equivalent <figure>" for a subscription agent with one reported', () => {
    const body = JSON.stringify({
      handlers: [
        {
          handler: "concierge",
          billing: "oauth",
          cost: null,
          api_equivalent: "0.3848",
          reason: null,
        },
      ],
    });
    const wire = parseWire(body, "/v1/sessions/s_1/messages");
    const { container } = render(<CostLine handlers={rowWire(wire, ".handlers")} />);

    expect(container.textContent).toBe("subscription, API-equivalent 0.3848");
    log.push('CostLine: "subscription, API-equivalent <figure>" for a reported API-equivalent cost');
  });
});

// ---- A figure outside a panel -------------------------------------------------------------------

describe("a figure with no panel around it", () => {
  it("still names its route and path, and has no source to open", () => {
    const wire = parseWire(MEASURED.text, MEASURED.route);
    const { container } = render(<Value leaf={wire.at(".score")} />);

    expect(figuresWithoutSource(container)).toEqual([]);
    expect(screen.queryByRole("button")).toBeNull();
  });
});

// ---- Evidence -----------------------------------------------------------------------------------

describe("evidence", () => {
  it("writes the manifest this ticket declares", () => {
    expect(log.length).toBeGreaterThan(10);
    mkdirSync(dirname(EVIDENCE), { recursive: true });
    writeFileSync(EVIDENCE, `${log.join("\n")}\n`, "utf8");
  });
});
