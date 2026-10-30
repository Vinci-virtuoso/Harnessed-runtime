// @vitest-environment jsdom
/**
 * FR-49 (`D-1`, `D-7`) and the `IdLookup` half of FR-60: the shared figures every screen composes
 * render what the gateway sent, in the order the gateway sent it, and nothing else.
 *
 * Eight components, each able to fail on its own:
 *   - `DataTable` keeps the gateway's row order and offers no way to change it;
 *   - `ScorePair` shows two read figures with a plain `->` and no third number between them;
 *   - `StatTile` wraps exactly one `Value` and names it;
 *   - `RuleBadge`, `KeyKindTag`, `StopReasonTag` print the word the wire spelled, in mono, with
 *     any gloss as separate visible text;
 *   - `PassMark` says "pass"/"fail" only where the field is `passed`, and the verbatim field
 *     everywhere else;
 *   - `IdLookup` sends the id exactly as typed and never asks whether it exists.
 *
 * The inputs are the recorded gateway responses (decision 33) wherever a recording holds the
 * value; the values no recording holds yet (`key_kind: "session"`, `stop_reason: "tool_use"`,
 * `"max_tokens"`, `null`, the five rules that are not `score`) are parsed from small bodies
 * written here, because FR-49 asks for *each known value* of each vocabulary and a recording of
 * the whole vocabulary does not exist. Nothing here edits a recorded fixture (MUST rule 24).
 *
 * "In mono" cannot be read off a computed style in jsdom, which loads no stylesheet, so the mono
 * claim is made against the component's own module CSS: the class a tag renders must set its
 * `font` from one of the mono type tokens. A tag that drops to the sans body font fails here.
 */
import { mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";

import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DataTable } from "../../components/DataTable";
import { IdLookup } from "../../components/IdLookup";
import { KeyKindTag } from "../../components/KeyKindTag";
import { PanelFrame, ResponseSource } from "../../components/PanelFrame";
import { WithRefresh } from "../support/with-refresh";
import { PassMark } from "../../components/PassMark";
import { RULE_GLOSSES, RuleBadge, UNDECIDED } from "../../components/RuleBadge";
import { ScorePair } from "../../components/ScorePair";
import { StatTile } from "../../components/StatTile";
import { StopReasonTag, TRUNCATED } from "../../components/StopReasonTag";
import { Value } from "../../components/Value";
import { GATEWAY_ORIGIN, fetchPanel, type RawResponse } from "../../lib/fetch-panel";
import { nullReasonFor } from "../../lib/null-reasons";
import { parseWire, type Leaf, type Wire } from "../../lib/wire";

import { figuresWithoutSource } from "../support/figures";
import { SLOW_SWEEP_MS } from "../support/slow-sweep";

const DASHBOARD = resolve(import.meta.dirname, "../..");

/** `PanelFrame`'s required source label, for a panel built here with nothing more specific to say. */
const SOURCE_LABEL = "Ledger: all time";
const GATEWAY_FIXTURES = resolve(DASHBOARD, "tests/fixtures/gateway");
const EVIDENCE = resolve(DASHBOARD, "../evidence/104/vocabulary.txt");

/** The type tokens that are mono, from `app/tokens.css`. A tag's font must come from one of them. */
const MONO_TYPES = ["--type-data", "--type-code", "--type-source", "--type-figure"];

/** The lines this run writes to `evidence/104/vocabulary.txt`. */
const log: string[] = [];

type Recorded = { name: string; route: string; code: number; text: string };

/** One recorded response, read from the committed record and never written by a test. */
function recorded(name: string): Recorded {
  const meta = JSON.parse(readFileSync(resolve(GATEWAY_FIXTURES, `${name}.meta.json`), "utf8")) as {
    route: string;
    status: number;
  };
  return {
    name,
    route: meta.route,
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

/** The parsed form of a recorded response, for the leaves a component is handed. */
function wireOf(body: Recorded): Wire {
  return parseWire(body.text, body.route);
}

/** A leaf of a body written here, for a vocabulary value no recording holds yet. */
function leafOf(text: string, path: string, route: string): Leaf {
  return parseWire(text, route).at(path);
}

// ---- The mono claim, read from the component's own stylesheet -----------------------------------

/** The declarations of the first rule whose selector is exactly `selector`. */
function block(css: string, selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = new RegExp(`(?:^|[{};])\\s*${escaped}\\s*\\{([^{}]*)\\}`).exec(
    css.replace(/\/\*[\s\S]*?\*\//g, ""),
  );
  if (!match) throw new Error(`no rule for ${selector}`);
  return match[1] ?? "";
}

/** Assert the class `component.module.css` gives this word sets its font from a mono token. */
function rendersInMono(component: string, className: string): void {
  const css = readFileSync(resolve(DASHBOARD, `components/${component}.module.css`), "utf8");
  const declarations = block(css, `.${className}`);
  const mono = MONO_TYPES.filter((token) => declarations.includes(token));
  expect(mono, `${component}.module.css .${className} is not mono`).not.toEqual([]);
}

// ---- A panel over a recorded response, for the figures that carry a source ---------------------

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

/** Stub `fetch` with one answer, and hand back the spy so a test can count the reads it made. */
function answersWith(next: Answer): ReturnType<typeof vi.fn> {
  const spy = vi.fn(async () => answer(next));
  vi.stubGlobal("fetch", spy);
  return spy;
}

/**
 * Everything an open drawer shows of the response a figure came from: the whole body, both edges
 * of the focus trap and the `curl` line's copy. A figure's source is only a source if opening it
 * works, so the walk is part of what "every figure shows where it came from" means here.
 */
function drawerHoldsTheResponse(dialog: HTMLElement, body: Recorded): void {
  const [expand] = within(dialog).queryAllByRole("button", { name: "Expand" });
  if (expand) {
    fireEvent.click(expand);
    expect(within(dialog).getByTestId("raw-body").textContent).toBe(body.text);
    fireEvent.click(within(dialog).getByRole("button", { name: "Collapse" }));
  } else {
    expect(within(dialog).getByTestId("raw-body").textContent).toBe(body.text);
  }

  const controls = within(dialog).getAllByRole("button");
  const first = controls.at(0);
  const last = controls.at(-1);
  last?.focus();
  fireEvent.keyDown(dialog, { key: "Tab" });
  expect(document.activeElement).toBe(first);
  fireEvent.keyDown(dialog, { key: "Tab", shiftKey: true });
  expect(document.activeElement).toBe(last);
  fireEvent.keyDown(dialog, { key: "ArrowDown" });
  first?.focus();
  fireEvent.keyDown(dialog, { key: "Tab" });
  expect(document.activeElement).toBe(first);

  fireEvent.click(within(dialog).getByRole("button", { name: "Copy curl" }));
}

/** Let React settle the panel's read; every state change here is a render, never a request. */
async function settle(): Promise<void> {
  await act(async () => {});
}

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(async () => answer({ status: 200, text: "{}" })));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

afterAll(() => {
  mkdirSync(dirname(EVIDENCE), { recursive: true });
  writeFileSync(EVIDENCE, `${log.join("\n")}\n`, "utf8");
});

// ---- `DataTable`: the gateway's order, and no way to change it ---------------------------------

describe("DataTable renders the gateway's rows in the gateway's order (D-1)", () => {
  const body = recorded("runs.listing");
  const rows = JSON.parse(body.text) as {
    runs: { run_key: string; key_kind: string; started_at: string }[];
  };
  /* The row identity is the array index, so the DOM order this table produces is the array order
     and nothing else: there is no key to sort by and no comparison to get wrong. */
  const indices = [...rows.runs.entries()].map(([index]) => index);

  function table(wire: Wire) {
    return (
      <DataTable
        caption="Loop runs"
        rows={indices}
        rowLink={{
          header: "run key",
          leaf: (index) => wire.at(`.runs[${index}].run_key`),
          href: (index) => `/runs/${wire.at(`.runs[${index}].run_key`).text}`,
        }}
        columns={[
          { header: "kind", cell: (index) => <KeyKindTag leaf={wire.at(`.runs[${index}].key_kind`)} /> },
          {
            header: "started",
            cell: (index) => <Value leaf={wire.at(`.runs[${index}].started_at`)} />,
          },
        ]}
      />
    );
  }

  it("puts the rows in the DOM in the response's array order", () => {
    const { container } = render(table(wireOf(body)));

    const cells = [...container.querySelectorAll("tbody tr")].map(
      (row) => row.querySelector("a")?.textContent ?? "",
    );

    expect(cells).toEqual(rows.runs.map((run) => run.run_key));
    log.push(`DataTable: ${cells.length} rows in the response's own order`);
  });

  it("is a semantic table whose every column header is a `th scope=col`", () => {
    const { container } = render(table(wireOf(body)));

    const table_ = container.querySelector("table");
    expect(table_).toBeTruthy();
    expect(container.querySelector("caption")?.textContent).toBe("Loop runs");
    const headers = [...container.querySelectorAll("thead th")];
    expect(headers.map((th) => th.getAttribute("scope"))).toEqual(["col", "col", "col"]);
    expect(headers.map((th) => th.textContent)).toEqual(["run key", "kind", "started"]);
  });

  it("links a row from its first cell's anchor and makes nothing else clickable", () => {
    const { container } = render(table(wireOf(body)));

    for (const row of container.querySelectorAll("tbody tr")) {
      const anchors = [...row.querySelectorAll("a")];
      expect(anchors).toHaveLength(1);
      expect(row.firstElementChild?.contains(anchors[0] ?? null)).toBe(true);
      expect(row.getAttribute("onclick")).toBeNull();
      expect(row.getAttribute("role")).toBeNull();
      // A control inside the link would be a nested interactive element no keyboard can reach.
      expect(anchors[0]?.querySelectorAll("a,button,input,select,textarea")).toHaveLength(0);
      expect(anchors[0]?.getAttribute("data-route")).toBe("/v1/runs");
    }
    const first = container.querySelector("tbody tr a");
    expect(first?.getAttribute("href")).toBe(`/runs/${rows.runs[0]?.run_key ?? ""}`);
    log.push("DataTable: the row link is the first cell's anchor and the row is not clickable");
  });

  it("offers no column sorting at all (D-1: the store's order is the order)", () => {
    const { container } = render(table(wireOf(body)));

    expect(container.querySelectorAll("th button")).toHaveLength(0);
    expect(container.querySelectorAll("[aria-sort]")).toHaveLength(0);
    expect(container.querySelectorAll("th a")).toHaveLength(0);
    expect(container.querySelectorAll("th [role='button']")).toHaveLength(0);
    log.push("DataTable: 0 sort controls, 0 aria-sort attributes");
  });

  it("renders a table with no rows as a table with no rows, never as a message", () => {
    const empty = recorded("empty.runs.listing");
    const { container } = render(
      <DataTable caption="Loop runs" rows={[]} columns={[{ header: "run key", cell: () => null }]} />,
    );

    expect(container.querySelectorAll("tbody tr")).toHaveLength(0);
    expect(container.querySelector("thead th")?.textContent).toBe("run key");
    expect(JSON.parse(empty.text)).toEqual({ runs: [] });
  });
});

// ---- `ScorePair`: two figures, no third number -------------------------------------------------

describe("ScorePair shows two read figures and computes nothing between them (D-1)", () => {
  const cycle = recorded("improve.cycle.promoted");

  it("joins them with a plain arrow and reads as 'score before 0.8, score after 1.0'", () => {
    const wire = wireOf(cycle);
    const { container } = render(
      <ScorePair before={wire.at(".score_before")} after={wire.at(".score_after")} />,
    );

    const figures = [...container.querySelectorAll("[data-figure]")].map((n) => n.textContent);
    expect(figures).toEqual(["0.8", "1.0"]);
    expect(screen.getByTestId("score-pair-join").textContent).toBe("->");
    expect(screen.getByRole("group").getAttribute("aria-label")).toBe(
      "score before 0.8, score after 1.0",
    );
    log.push('ScorePair: "0.8 -> 1.0", announced as "score before 0.8, score after 1.0"');
  });

  it("shows no delta, no coloured arrow and no up or down glyph", () => {
    const wire = wireOf(cycle);
    const { container } = render(
      <ScorePair before={wire.at(".score_before")} after={wire.at(".score_after")} />,
    );

    const text = container.textContent ?? "";
    // 0.8 -> 1.0 is a rise of 0.2 and a rise of 25%. Neither number may be anywhere in the DOM.
    for (const derived of ["0.2", "25%", "+0.2", "0.20"]) expect(text).not.toContain(derived);
    for (const glyph of ["↑", "↓", "▲", "▼", "⬆", "⬇"]) {
      expect(text).not.toContain(glyph);
    }
    expect(container.querySelectorAll("[data-direction]")).toHaveLength(0);
    expect(container.innerHTML).not.toMatch(/color-(pass|fail|warn)/);
    log.push("ScorePair: 0 deltas, 0 direction glyphs, 0 colour by direction");
  });

  it("renders a null side as `unknown` with its documented reason, and says so out loud", () => {
    const undecided = recorded("improve.cycle.undecided");
    const wire = wireOf(undecided);
    const { container } = render(
      <ScorePair before={wire.at(".score_before")} after={wire.at(".score_after")} />,
    );

    const figures = [...container.querySelectorAll("[data-figure]")].map((n) => n.textContent);
    expect(figures).toEqual(["unknown", "unknown"]);
    expect(container.textContent).toContain(
      nullReasonFor(undecided.route, ".score_before") ?? "missing reason",
    );
    expect(screen.getByRole("group").getAttribute("aria-label")).toBe(
      "score before unknown, score after unknown",
    );
    expect(container.textContent).not.toContain("0");
  });

  it("renders one measured side beside one unknown side, and still computes nothing", () => {
    const half = leafOf('{"score_before":0.8,"score_after":null}', ".score_after", "/v1/eval/compare");
    const measured = leafOf(
      '{"score_before":0.8,"score_after":null}',
      ".score_before",
      "/v1/eval/compare",
    );
    const { container } = render(<ScorePair before={measured} after={half} />);

    expect([...container.querySelectorAll("[data-figure]")].map((n) => n.textContent)).toEqual([
      "0.8",
      "unknown",
    ]);
    expect(screen.getByRole("group").getAttribute("aria-label")).toBe(
      "score before 0.8, score after unknown",
    );
    log.push("ScorePair: one measured side beside one unknown side, and still 0 arithmetic");
  });
});

// ---- `StatTile`: exactly one `Value`, named and described --------------------------------------

describe("StatTile wraps exactly one Value and names it", () => {
  const health = recorded("health");

  it("has the label as its accessible name, and the source path as a non-rendered hook only", () => {
    const wire = wireOf(health);
    const { container } = render(<StatTile label="schema version" leaf={wire.at(".schema_version")} />);

    const tile = screen.getByRole("group", { name: "schema version" });
    // No jq path caption is drawn any more (`D-9`): the path travels only as `data-path`, never
    // read into text or an accessible description.
    expect(tile.getAttribute("aria-describedby")).toBeNull();
    expect(container.textContent).not.toContain(".schema_version");
    expect(container.querySelectorAll("[data-figure]")).toHaveLength(1);
    const figure = container.querySelector("[data-figure]");
    expect(figure?.getAttribute("data-path")).toBe(".schema_version");
    expect(figure?.textContent).toBe(wire.at(".schema_version").text);
    log.push("StatTile: 1 Value, named by its label, its source path a non-rendered hook only");
  });

  it("renders an absent figure as unknown with its reason, never as a zero", () => {
    const zeroCase = recorded("eval.run.zero-case");
    const wire = wireOf(zeroCase);
    const reason = nullReasonFor(zeroCase.route, ".score");
    const { container } = render(
      <StatTile label="score" leaf={wire.at(".score")} nullReason={reason} />,
    );

    expect(container.querySelectorAll("[data-figure]")).toHaveLength(1);
    expect(container.querySelector("[data-figure]")?.textContent).toBe("unknown");
    expect(container.textContent).toContain(reason ?? "missing reason");
  });
});

// ---- The verbatim vocabularies (`D-7`) ---------------------------------------------------------

describe("every known key_kind renders verbatim in mono", () => {
  const route = "/v1/runs";
  const body = '{"runs":[{"key_kind":"session"},{"key_kind":"case_run"}]}';

  /**
   * `key_kind`'s two rows in `HUMAN_WORDS` (`lib/human-words.ts`): from v5 both render as their
   * human text on screen, with the ledger's own word kept reachable only in the figure's tooltip
   * (via `Value`'s `human` prop), never as the visible text any more.
   */
  const HUMAN: Record<string, string> = { session: "Chat", case_run: "Eval case" };

  it.each(Object.entries(HUMAN))(
    "renders %s in human words, with the ledger's own word still reachable",
    (kind, text) => {
      const index = kind === "session" ? "0" : "1";
      const leaf = leafOf(body, `.runs[${index}].key_kind`, route);
      const response: RawResponse = {
        url: route,
        status: "200",
        bytes: "0",
        text: body,
        fetchedAt: new Date(),
      };
      const { container } = render(
        <ResponseSource response={response} method="GET" sourceLabel={SOURCE_LABEL}>
          <KeyKindTag leaf={leaf} />
        </ResponseSource>,
      );

      expect(container.textContent).toBe(text);
      const figure = container.querySelector("[data-figure]");
      expect(figure?.textContent).toBe(text);
      expect(figure?.getAttribute("title")).toContain(kind);
      log.push(`KeyKindTag: ${kind} -> "${text}", the ledger's own word in the tooltip`);
    },
  );

  it("is mono and carries no colour coding of its own", () => {
    rendersInMono("KeyKindTag", "tag");
    const css = readFileSync(resolve(DASHBOARD, "components/KeyKindTag.module.css"), "utf8");
    expect(css).not.toMatch(/--color-(pass|fail|warn|accent)/);
  });
});

describe("every known stop_reason renders verbatim in mono", () => {
  const route = "/v1/runs";
  const known = ["end_turn", "tool_use", "max_tokens", "pause_turn", "refusal"];
  const body = JSON.stringify({ reasons: known.map((name) => ({ stop_reason: name })) });

  /**
   * `stop_reason`'s rows `HUMAN_WORDS` knows among `known` above (`lib/human-words.ts`): from v5
   * each renders as its human text on screen, with the ledger's own word kept reachable only in
   * the figure's tooltip. `pause_turn` and `refusal` are not in the table, so they are unaffected
   * and still render verbatim below.
   */
  const HUMAN: Record<string, string> = {
    end_turn: "Finished",
    tool_use: "Stopped to use a tool",
    max_tokens: "Cut off at the token limit",
  };
  /** The words genuinely absent from `HUMAN_WORDS`: `humanWord` finds no row, so the fallback holds. */
  const stillVerbatim = known.filter((name) => HUMAN[name] === undefined);

  function sourcedTag(name: string): { container: HTMLElement } {
    const index = known.indexOf(name);
    const leaf = leafOf(body, `.reasons[${index}].stop_reason`, route);
    const response: RawResponse = { url: route, status: "200", bytes: "0", text: body, fetchedAt: new Date() };
    return render(
      <ResponseSource response={response} method="GET" sourceLabel={SOURCE_LABEL}>
        <StopReasonTag leaf={leaf} />
      </ResponseSource>,
    );
  }

  it.each(Object.entries(HUMAN))(
    "renders %s in human words, with the ledger's own word still reachable",
    (name, text) => {
      const { container } = sourcedTag(name);

      expect(container.querySelector("[data-figure]")?.textContent).toBe(text);
      const figure = container.querySelector("[data-figure]");
      expect(figure?.getAttribute("title")).toContain(name);
      log.push(`StopReasonTag: ${name} -> "${text}", the ledger's own word in the tooltip`);
    },
  );

  it.each(stillVerbatim)("renders %s as itself", (name) => {
    const index = known.indexOf(name);
    const { container } = render(
      <StopReasonTag leaf={leafOf(body, `.reasons[${index}].stop_reason`, route)} />,
    );

    expect(container.querySelector("[data-figure]")?.textContent).toBe(name);
    log.push(`StopReasonTag: ${name}`);
  });

  it("spells max_tokens' meaning out in text beside the word, never in colour alone", () => {
    const { container } = sourcedTag("max_tokens");

    expect(container.querySelector("[data-figure]")?.textContent).toBe("Cut off at the token limit");
    expect(container.querySelector("[data-figure]")?.getAttribute("title")).toContain("max_tokens");
    expect(TRUNCATED).toBe("truncated");
    expect(screen.getByText(TRUNCATED)).toBeTruthy();
    rendersInMono("StopReasonTag", "tag");
    log.push('StopReasonTag: max_tokens carries the word "truncated" in text');
  });

  it("says truncated on max_tokens and on nothing else", () => {
    for (const name of known) {
      if (name === "max_tokens") continue;
      const index = known.indexOf(name);
      const { container } = render(
        <StopReasonTag leaf={leafOf(body, `.reasons[${index}].stop_reason`, "/v1/runs")} />,
      );
      expect(container.textContent).not.toContain(TRUNCATED);
      cleanup();
    }
  });

  it("renders a null stop_reason as unknown with the build's own reason, never as a word", () => {
    const route = "/v1/runs";
    const leaf = leafOf('{"runs":[{"stop_reason":null}]}', ".runs[0].stop_reason", route);
    const reason = nullReasonFor(route, ".runs[0].stop_reason");
    const { container } = render(<StopReasonTag leaf={leaf} nullReason={reason} />);

    expect(container.querySelector("[data-figure]")?.textContent).toBe("unknown");
    expect(container.textContent).toContain("recorded before Harnessed measured this");
    log.push("StopReasonTag: null is unknown plus the operator-terms sentence");
  });
});

describe("every known rule renders verbatim in mono with its gloss as separate visible text", () => {
  const rules = ["score", "tiebreak", "no_gain", "regression", "missing_case", "undersampled"];
  const body = JSON.stringify({ cycles: rules.map((rule) => ({ rule })) });

  it.each(rules)("renders %s as itself, with a gloss beside it", (rule) => {
    const index = rules.indexOf(rule);
    const route = "/v1/improve/cycles/imp_1";
    const { container } = render(
      <RuleBadge leaf={leafOf(body, `.cycles[${index}].rule`, route)} />,
    );

    expect(container.querySelector("[data-figure]")?.textContent).toBe(rule);
    const gloss = screen.getByTestId("rule-gloss");
    expect(gloss.textContent).toBe(RULE_GLOSSES[rule]);
    expect(gloss.textContent).not.toBe("");
    // The gloss is visible text and not a tooltip attribute.
    expect(gloss.getAttribute("title")).toBeNull();
    expect(gloss.getAttribute("hidden")).toBeNull();
    log.push(`RuleBadge: ${rule}`);
  });

  it("has a gloss for every rule the promoter can decide on, and no others", () => {
    expect(Object.keys(RULE_GLOSSES).sort()).toEqual([...rules].sort());
  });

  it("renders the empty rule as 'no decision reached' and shows no word", () => {
    const undecided = recorded("improve.cycle.undecided");
    const wire = wireOf(undecided);
    const { container } = render(<RuleBadge leaf={wire.at(".rule")} />);

    expect(UNDECIDED).toBe("no decision reached");
    expect(container.textContent).toBe(UNDECIDED);
    expect(container.querySelectorAll("[data-figure]")).toHaveLength(0);
    log.push('RuleBadge: rule "" reads "no decision reached"');
  });

  it("renders the rule the recorded promoted cycle decided on", () => {
    const wire = wireOf(recorded("improve.cycle.promoted"));
    const { container } = render(<RuleBadge leaf={wire.at(".rule")} />);

    expect(container.querySelector("[data-figure]")?.textContent).toBe("score");
    rendersInMono("RuleBadge", "word");
  });
});

describe("event names and error class names render verbatim in mono", () => {
  it("prints every recorded event name exactly as the ledger spelled it", () => {
    const body = recorded("sessions.events");
    const wire = wireOf(body);
    const names = (JSON.parse(body.text) as { events: { name: string }[] }).events.map(
      (event) => event.name,
    );

    const rendered = names.map((_, index) => {
      const { container } = render(<Value leaf={wire.at(`.events[${index}].name`)} />);
      const text = container.querySelector("[data-figure]")?.textContent ?? "";
      cleanup();
      return text;
    });

    expect(rendered).toEqual(names);
    rendersInMono("Value", "figure");
    log.push(`event names: ${rendered.length} rendered exactly as sent`);
  });

  it("prints every recorded error class name exactly as the gateway spelled it", () => {
    const classes: string[] = [];
    for (const body of everyRecordedBody()) {
      if (body.code === 200) continue;
      const parsed = JSON.parse(body.text) as { error?: string };
      if (parsed.error === undefined) continue;
      const { container } = render(<Value leaf={wireOf(body).at(".error")} />);
      expect(container.querySelector("[data-figure]")?.textContent).toBe(parsed.error);
      classes.push(parsed.error);
      cleanup();
    }

    expect(classes).toContain("SurfaceNotFound");
    expect(classes).toContain("LimitError");
    expect(classes).toContain("StorageError");
    log.push(`error class names: ${classes.length} rendered exactly as sent`);
  });
});

// ---- `PassMark`: "pass" only where the field is `passed` ---------------------------------------

describe("PassMark says pass or fail only for `passed` (D-7)", () => {
  const run = recorded("eval.run.measured");

  it("shows pass with a glyph for a case that passed, and the word is always there", () => {
    const { container } = render(<PassMark leaf={wireOf(run).at(".cases[0].passed")} />);

    expect(screen.getByTestId("pass-mark-word").textContent).toBe("pass");
    expect(screen.getByTestId("pass-mark-glyph").textContent).not.toBe("");
    expect(container.textContent).toContain("pass");
    log.push("PassMark: .cases[0].passed renders pass with a glyph");
  });

  it("shows fail with a different glyph for a case that failed", () => {
    const { container } = render(<PassMark leaf={wireOf(run).at(".cases[2].passed")} />);

    expect(screen.getByTestId("pass-mark-word").textContent).toBe("fail");
    expect(container.textContent).toContain("fail");
  });

  it("never carries status in colour alone: the word is part of the accessible name", () => {
    render(<PassMark leaf={wireOf(run).at(".cases[2].passed")} />);

    const mark = screen.getByTestId("pass-mark");
    expect(mark.textContent).toContain("fail");
    expect(mark.getAttribute("aria-hidden")).toBeNull();
    expect(screen.getByTestId("pass-mark-glyph").getAttribute("aria-hidden")).toBe("true");
  });

  it("shows `promoted` as the verbatim field and never as pass or fail", () => {
    const wire = wireOf(recorded("improve.cycle.undecided"));
    const { container } = render(<PassMark leaf={wire.at(".promoted")} />);

    expect(container.textContent).toBe("promoted: false");
    expect(container.textContent).not.toContain("pass");
    expect(container.textContent).not.toContain("fail");
    expect(container.querySelector("[data-figure]")?.textContent).toBe("false");
    log.push("PassMark: promoted renders `promoted: false`, never pass or fail");
  });

  it("shows a promoted cycle's own `promoted: true`, still not as a pass", () => {
    const wire = wireOf(recorded("improve.cycle.promoted"));
    const { container } = render(<PassMark leaf={wire.at(".promoted")} />);

    expect(container.textContent).toBe("promoted: true");
    expect(container.textContent).not.toContain("pass");
  });

  it("shows `ok` as the verbatim field too", () => {
    const { container } = render(<PassMark leaf={leafOf('{"ok":true}', ".ok", "/v1/health")} />);

    expect(container.textContent).toBe("ok: true");
    expect(container.textContent).not.toContain("pass");
    rendersInMono("PassMark", "word");
    log.push("PassMark: ok renders `ok: true`");
  });

  it("renders every recorded `passed` in one run, in the run's own order", () => {
    const wire = wireOf(run);
    const cases = (JSON.parse(run.text) as { cases: { passed: boolean }[] }).cases;

    const words = cases.map((_, index) => {
      render(<PassMark leaf={wire.at(`.cases[${index}].passed`)} />);
      const word = screen.getByTestId("pass-mark-word").textContent ?? "";
      cleanup();
      return word;
    });

    expect(words).toEqual(cases.map((one) => (one.passed ? "pass" : "fail")));
    log.push(`PassMark: ${words.length} recorded cases, in the run's own order`);
  });
});

// ---- `IdLookup`: the id exactly as typed, existence never checked (FR-60) ----------------------

const pushed: string[] = [];

vi.mock("next/navigation", () => ({
  useRouter: () => ({
    push: (href: string) => {
      pushed.push(href);
    },
  }),
}));

describe("IdLookup opens an id exactly as typed and never checks it exists (FR-60)", () => {
  const hrefFor = (id: string) => `/improve/${id}`;

  beforeEach(() => {
    pushed.length = 0;
  });

  it("names the kind of id it expects in a visible label", () => {
    render(<IdLookup kind="improvement run id" hrefFor={hrefFor} />);

    const input = screen.getByLabelText("improvement run id");
    expect(input.tagName).toBe("INPUT");
    expect(screen.getByText("improvement run id")).toBeTruthy();
    log.push("IdLookup: the visible label names the id kind it expects");
  });

  it("disables Open while the input is blank, and enables it once an id is typed", () => {
    render(<IdLookup kind="session id" hrefFor={(id) => `/sessions/${id}`} />);

    const open = screen.getByRole("button", { name: "Open" }) as HTMLButtonElement;
    expect(open.disabled).toBe(true);

    fireEvent.change(screen.getByLabelText("session id"), { target: { value: "sess_1" } });
    expect(open.disabled).toBe(false);

    fireEvent.change(screen.getByLabelText("session id"), { target: { value: "" } });
    expect(open.disabled).toBe(true);
    log.push("IdLookup: Open is disabled while the input is blank");
  });

  it("navigates nowhere while blank, however hard it is submitted", () => {
    const { container } = render(<IdLookup kind="session id" hrefFor={hrefFor} />);

    fireEvent.submit(container.querySelector("form") as HTMLFormElement);
    fireEvent.click(screen.getByRole("button", { name: "Open" }));

    expect(pushed).toEqual([]);
  });

  it("sends the id exactly as typed on Enter: no trim, no split, no reformat", () => {
    render(<IdLookup kind="eval run id" hrefFor={(id) => `/evals/${id}`} />);
    // A case run key with its colons, wrapped in the spaces a paste brings with it.
    const typed = "  eval_000000000HB774KP7M6WX8HT48:holdout-003-iso-date:1  ";

    const input = screen.getByLabelText("eval run id");
    fireEvent.change(input, { target: { value: typed } });
    fireEvent.submit(input.closest("form") as HTMLFormElement);

    expect(pushed).toEqual([`/evals/${typed}`]);
    log.push("IdLookup: Enter sends the id with its spaces and colons untouched");
  });

  it("sends the same id on Open as on Enter", () => {
    render(<IdLookup kind="improvement run id" hrefFor={hrefFor} />);
    const typed = "imp_000000000J7TMSF08D899W4ZWR";

    fireEvent.change(screen.getByLabelText("improvement run id"), { target: { value: typed } });
    fireEvent.click(screen.getByRole("button", { name: "Open" }));

    expect(pushed).toEqual([`/improve/${typed}`]);
  });

  it("asks the gateway nothing: existence is the target page's 404 to report", () => {
    const spy = answersWith({ status: 200, text: "{}" });
    render(<IdLookup kind="improvement run id" hrefFor={hrefFor} />);

    fireEvent.change(screen.getByLabelText("improvement run id"), { target: { value: "imp_nope" } });
    fireEvent.click(screen.getByRole("button", { name: "Open" }));

    expect(spy).not.toHaveBeenCalled();
    expect(pushed).toEqual(["/improve/imp_nope"]);
    log.push("IdLookup: 0 requests made; existence is reported by the target page's 404");
  });
});

// ---- Inside a panel, every figure still names its source (FR-51) -------------------------------

describe("the vocabulary inside a panel keeps every figure's source", () => {
  it("gives each half of a ScorePair its own source, a figure over the panel's response", async () => {
    const cycle = recorded("improve.cycle.promoted");
    answersWith({ status: 200, text: cycle.text });
    const { container } = render(
      <PanelFrame route={cycle.route} title="Cycle" sourceLabel={SOURCE_LABEL}>
        {(wire) => (
          <>
            <ScorePair before={wire.at(".score_before")} after={wire.at(".score_after")} />
            <RuleBadge leaf={wire.at(".rule")} />
            <PassMark leaf={wire.at(".promoted")} />
            <StatTile label="scored" leaf={wire.at(".scored")} />
          </>
        )}
      </PanelFrame>,
    );
    await settle();

    // No separate chip any more (`SourceChip` is orphaned): the figure is its own trigger.
    const figures = [...container.querySelectorAll(`[data-figure][data-route="${cycle.route}"]`)];
    expect(figures.length).toBeGreaterThanOrEqual(2);
    expect(figuresWithoutSource(container)).toEqual([]);

    fireEvent.click(figures[0] as HTMLElement);
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText(SOURCE_LABEL)).toBeTruthy();
    expect(within(dialog).getByTestId("record-view")).toBeTruthy();
    expect(within(dialog).getByTestId("cited")).toBeTruthy();
    fireEvent.keyDown(dialog, { key: "Escape" });
    await settle();
    log.push("panel: every vocabulary figure is its own trigger over the panel's response");
  });

  it("renders a recorded listing as a table inside a panel, in the gateway's order", async () => {
    const body = recorded("runs.listing");
    const rows = (JSON.parse(body.text) as { runs: { run_key: string }[] }).runs;
    answersWith({ status: 200, text: body.text });
    const { container } = render(
      <PanelFrame route={body.route} title="Runs" sourceLabel={SOURCE_LABEL}>
        {(wire) => (
          <DataTable
            caption="Loop runs"
            rows={[...rows.entries()].map(([index]) => index)}
            rowLink={{
              header: "run key",
              leaf: (index) => wire.at(`.runs[${index}].run_key`),
              href: (index) => `/runs/${wire.at(`.runs[${index}].run_key`).text}`,
            }}
            columns={[
              {
                header: "stop reason",
                cell: (index) => (
                  <StopReasonTag
                    leaf={wire.at(`.runs[${index}].stop_reason`)}
                    nullReason={nullReasonFor(body.route, `.runs[${index}].stop_reason`)}
                  />
                ),
              },
            ]}
          />
        )}
      </PanelFrame>,
    );
    await settle();

    const anchors = [...container.querySelectorAll("tbody tr td:first-child a")].map(
      (a) => a.textContent,
    );
    expect(anchors).toEqual(rows.map((run) => run.run_key));
    expect(figuresWithoutSource(container)).toEqual([]);
    log.push(`panel: ${anchors.length} table rows in the gateway's order, each figure sourced`);
  });

  it("renders the seven panel states around the vocabulary without inventing a figure", async () => {
    const states: Recorded[] = [
      recorded("health"),
      recorded("empty.runs.listing"),
      recorded("notfound.runs.run"),
      recorded("badrequest.runs.limit"),
      recorded("nodatabase.runs.listing"),
    ];

    for (const state of states) {
      answersWith({ status: state.code, text: state.text });
      const { container } = render(
        <PanelFrame route={state.route} title="Panel" sourceLabel={SOURCE_LABEL} emptyText="no session has run yet">
          {(wire) =>
            state.name === "health" ? (
              <StatTile label="route count" leaf={wire.at(".route_count")} />
            ) : null
          }
        </PanelFrame>,
      );
      await settle();

      expect(figuresWithoutSource(container)).toEqual([]);
      cleanup();
    }

    log.push(`panel: ${states.length} states rendered, 0 figures without a source`);
  });
});

// ---- A read that failed renders no vocabulary at all -------------------------------------------

describe("a panel that could not be read invents no figure and no word", () => {
  it("renders the unreachable state, 0 figures and 0 tags", async () => {
    const body = recorded("runs.listing");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    );
    const { container } = render(
      <PanelFrame route={body.route} title="Panel" sourceLabel={SOURCE_LABEL}>
        {(wire) => <StatTile label="anything" leaf={wire.at(".error")} />}
      </PanelFrame>,
    );
    await settle();

    expect(screen.getByRole("heading", { name: "Gateway unreachable" })).toBeTruthy();
    expect(container.querySelectorAll("[data-figure]")).toHaveLength(0);
    expect(container.querySelectorAll("table")).toHaveLength(0);
    expect(container.textContent).not.toContain(GATEWAY_ORIGIN);
    log.push("panel: an unreachable read renders 0 figures and 0 vocabulary tags");
  });

  it("names a rejected browser fetch by its own words, and invents no status", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    );

    const result = await fetchPanel("/v1/runs", GATEWAY_ORIGIN);

    expect(result.state).toBe("unreachable");
    if (result.state !== "unreachable") return;
    expect(result.cause).toBe("Failed to fetch");
  });
});

// ---- A table keeps its order as the panel ages and is read again --------------------------------

describe("a table's order is the response's, at every age of the panel", () => {
  const SECOND = 1000;
  const MINUTE = 60 * SECOND;
  const FROZEN = new Date("2026-09-17T14:03:09");

  it("holds the same row order after five minutes and after a Refetch", async () => {
    vi.useFakeTimers({
      toFake: ["Date", "setTimeout", "clearTimeout", "setInterval", "clearInterval"],
    });
    vi.setSystemTime(FROZEN);
    try {
      const body = recorded("runs.listing");
      const rows = (JSON.parse(body.text) as { runs: { run_key: string }[] }).runs;
      const spy = answersWith({ status: 200, text: body.text });
      const { container } = render(
        <WithRefresh>
        <PanelFrame route={body.route} title="Runs" sourceLabel={SOURCE_LABEL}>
          {(wire) => (
            <DataTable
              caption="Loop runs"
              rows={[...rows.entries()].map(([index]) => index)}
              rowLink={{
                header: "run key",
                leaf: (index) => wire.at(`.runs[${index}].run_key`),
                href: (index) => `/runs/${wire.at(`.runs[${index}].run_key`).text}`,
              }}
              columns={[
                {
                  header: "kind",
                  cell: (index) => <KeyKindTag leaf={wire.at(`.runs[${index}].key_kind`)} />,
                },
              ]}
            />
          )}
        </PanelFrame>
        </WithRefresh>,
      );
      const order = async () => {
        await act(async () => {
          await vi.advanceTimersByTimeAsync(0);
        });
        return [...container.querySelectorAll("tbody tr td:first-child a")].map((a) => a.textContent);
      };

      const wanted = rows.map((run) => run.run_key);
      expect(await order()).toEqual(wanted);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(5 * MINUTE);
      });
      expect(screen.getByTestId("stale-badge").textContent).toBe("Updated 5 min ago");
      expect(screen.getByTestId("stale-badge").getAttribute("data-warn")).toBe("true");
      expect(await order()).toEqual(wanted);

      const [refetch] = screen.getAllByRole("button", { name: "Refresh" });
      if (refetch) fireEvent.click(refetch);
      expect(await order()).toEqual(wanted);
      expect(screen.getByTestId("stale-badge").getAttribute("data-warn")).toBe("false");
      expect(spy).toHaveBeenCalledTimes(2);
      log.push("DataTable: the row order is the same fresh, stale and refetched");
    } finally {
      cleanup();
      vi.useRealTimers();
    }
  });
});

// ---- The recorded corpus, swept for a figure that disagrees with its body ----------------------

describe("every recorded response renders its vocabulary and nothing it worked out", () => {
  it("renders each response's first figure inside a panel with its source intact", async () => {
    const bodies = everyRecordedBody();
    const sourced: string[] = [];

    for (const body of bodies) {
      answersWith({ status: body.code, text: body.text });
      const { container } = render(
        <PanelFrame route={body.route} title="Panel" sourceLabel={SOURCE_LABEL}>
          {(wire) => {
            const path = firstLeafPath(body.text);
            return path === undefined ? null : (
              <StatTile
                label="first figure"
                leaf={wire.at(path)}
                nullReason={nullReasonFor(body.route, path)}
              />
            );
          }}
        </PanelFrame>,
      );
      await settle();

      expect(figuresWithoutSource(container), `${body.name} rendered a sourceless figure`).toEqual(
        [],
      );
      const [chip] = screen.queryAllByRole("button", { name: `Source: GET ${body.route}` });
      if (chip) {
        sourced.push(body.name);
        fireEvent.click(chip);
        const dialog = screen.getByRole("dialog");
        drawerHoldsTheResponse(dialog, body);
        fireEvent.keyDown(dialog, { key: "Escape" });
        await settle();
        expect(screen.queryByRole("dialog")).toBeNull();
      }
      cleanup();
    }

    expect(bodies.length).toBeGreaterThan(0);
    log.push(
      `corpus: ${bodies.length} recorded responses rendered through the shared figures, ${sourced.length} with a source chip`,
    );
  // The whole recorded corpus in one test: about 50 renders, each with a drawer open and close.
  // It passes in a second or two alone and takes far longer when the suite fills every worker, so
  // the ceiling is contention, not the sweep (ticket 118). Widen the ceiling rather than thinning
  // what is swept.
  }, SLOW_SWEEP_MS);
});

/** The jq path of the first scalar in a body, in document order, or nothing where it holds none. */
function firstLeafPath(text: string): string | undefined {
  let found: string | undefined;
  const walk = (node: unknown, path: string): void => {
    if (found !== undefined) return;
    if (Array.isArray(node)) {
      for (const [index, item] of node.entries()) walk(item, `${path}[${index}]`);
      return;
    }
    if (typeof node === "object" && node !== null) {
      for (const [key, item] of Object.entries(node as Record<string, unknown>)) {
        walk(item, `${path}.${key}`);
      }
      return;
    }
    found = path;
  };
  walk(JSON.parse(text), "");
  return found;
}
