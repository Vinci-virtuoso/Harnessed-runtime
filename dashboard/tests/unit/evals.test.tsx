// @vitest-environment jsdom
/**
 * FR-45, FR-56, FR-57, FR-64, FR-129 (UJ-8, UJ-9): `/evals` holds the eval runs, the comparison
 * of any two and the improvement history (ticket 207). This file is the union of the former
 * `evals`, `evals-compare` and `improve` test files, each body kept whole in its own scope, plus
 * the consolidation's own criteria at the foot. Every page is rendered inside `AppShell`.
 */
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";

import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CliCommand, COPY_LABEL } from "../../app/evals/CliCommand";
import { CycleLookup } from "../../app/evals/CycleLookup";
import {
  NEEDS_BOTH,
  NO_MOVEMENT,
  ONLY_MOVED,
  PANEL_TITLE as COMPARE_TITLE,
  ComparePanel,
} from "../../app/evals/ComparePanel";
import { CyclePanel, NO_CANDIDATE, NO_STEPS, PANEL_TITLE as CYCLE_TITLE } from "../../app/evals/CyclePanel";
import { EvalRunPanel, NO_CASES, REPLAYED_TAG } from "../../app/evals/EvalRunPanel";
import { EvalRunsPanel } from "../../app/evals/EvalRunsPanel";
import { PANEL_TITLE as PROMOTIONS_TITLE, PromotionsPanel } from "../../app/evals/PromotionsPanel";
import { FAKE_GLOSS } from "../../app/evals/ProviderLine";
import {
  DEFAULT_LIMIT,
  LIMITS,
  PROMOTE_COMMAND,
  RUN_COMMAND,
  RUN_JSON_COMMAND,
  compareHref,
  cycleHref,
  cycleRoute,
  evalRunHref,
  footerLine,
  listingHref,
  promotionsFooterLine,
  promotionsRoute,
} from "../../app/evals/limit";
import EvalsPage, { LOOKUP_NOTE, PROMOTE_NOTE } from "../../app/evals/page";
import { COMPARE_ROUTE, SIDES, compareRoute, sideHref } from "../../app/evals/query";
import { AppShell } from "../../components/AppShell";
import { RULE_GLOSSES, UNDECIDED } from "../../components/RuleBadge";
import { Value } from "../../components/Value";
import { cliTwinFor } from "../../lib/cli-twins";
import { GATEWAY_ORIGIN, gatewayHref } from "../../lib/fetch-panel";
import { absentTextFor, emptyTextFor, nullReasonFor } from "../../lib/null-reasons";
import { parseWire } from "../../lib/wire";
import CompareForward from "../../app/evals/compare/page";
import ImproveForward from "../../app/improve/page";
import { pageAt, useSearchParams } from "./support/query";

import { figuresWithoutSource } from "../support/figures";
import { divergences, renderedFigures, sourceCharactersByPath } from "../support/source-at";

import type { ReactNode } from "react";

/** Where a limit choice or a lookup sent the operator, shared by every scope below. */
const pushed: string[] = [];

vi.mock("next/navigation", async (importOriginal) => ({
  ...((await importOriginal()) as object),
  useSearchParams: (await import("./support/query")).useSearchParams,
  useRouter: () => ({
    push: (href: string) => {
      pushed.push(href);
    },
    replace: (href: string) => {
      pushed.push(href);
    },
  }),
}));

/*
 * The three former pages' own views, each the old page's heading and panels and nothing else, so
 * the bodies below keep every assertion they made while `/evals` was three pages. The merged page
 * itself is exercised by the "ticket 207" scope at the foot.
 */
function EvalRunsView() {
  const query = useSearchParams();
  const limit = query.get("limit") ?? undefined;
  const id = query.get("id") ?? undefined;
  if (id !== undefined) {
    return (
      <div>
        <h1>Eval run</h1>
        <EvalRunPanel evalRunId={id} />
      </div>
    );
  }
  return (
    <div>
      <h1>Eval runs</h1>
      <EvalRunsPanel limit={limit ?? DEFAULT_LIMIT} />
    </div>
  );
}

function CompareView() {
  const query = useSearchParams();
  return (
    <div>
      <h1>Compare eval runs</h1>
      <ComparePanel before={query.get("before") ?? undefined} after={query.get("after") ?? undefined} />
    </div>
  );
}

function ImproveView() {
  const query = useSearchParams();
  const limit = query.get("limit") ?? undefined;
  const id = query.get("id") ?? undefined;
  if (id !== undefined) {
    return (
      <div>
        <h1>Improvement cycle</h1>
        <CyclePanel improvementRunId={id} />
        <CliCommand note={PROMOTE_NOTE} command={PROMOTE_COMMAND} />
      </div>
    );
  }
  return (
    <div>
      <h1>Improvement</h1>
      <PromotionsPanel limit={limit ?? DEFAULT_LIMIT} />
      <section aria-label="Open an improvement cycle">
        <CliCommand note={LOOKUP_NOTE} command={RUN_JSON_COMMAND} />
        <CycleLookup />
      </section>
      <CliCommand note={PROMOTE_NOTE} command={PROMOTE_COMMAND} />
    </div>
  );
}

describe("eval runs (ticket 107)", () => {
const DASHBOARD = resolve(import.meta.dirname, "../..");
const REPO = resolve(DASHBOARD, "..");
const GATEWAY_FIXTURES = resolve(DASHBOARD, "tests/fixtures/gateway");
const EVIDENCE = resolve(REPO, "evidence/107/evals.txt");
const EVALS_HTML = resolve(REPO, "evidence/107/evals.html");
const DETAIL_HTML = resolve(REPO, "evidence/107/eval-detail.html");
const ZERO_CASE_HTML = resolve(REPO, "evidence/107/zero-case.html");

/** The gateway this test's shell is pointed at. Not the operator's 8756 (decision 30). */
const GATEWAY = GATEWAY_ORIGIN;

/** The lines this run writes to `evidence/107/evals.txt`. */
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

const listing = recorded("eval.runs.listing");
const emptyListing = recorded("empty.eval.runs.listing");
const measured = recorded("eval.run.measured");
const zeroCase = recorded("eval.run.zero-case");
const noSuchRun = recorded("notfound.eval.run");
const health = recorded("health");

/** The three ids the recordings hold, and the id none of them does. */
const MEASURED_ID = "eval_0000000005AVYXNCY6ZK6K3CP9";
const ZERO_CASE_ID = "eval_recorded_zero";
const BEFORE_ID = "eval_recorded_before";
const AFTER_ID = "eval_recorded_after";
const UNKNOWN_ID = "eval_00000000000000000000000000";

/**
 * A run holding one case the evaluator scored fresh. Every recorded run replayed every case from
 * the ledger, so "the tag is on the replayed case and not on the other one" has nothing it could
 * fail against without a body that holds both (MUST rule 12).
 */
const MIXED_RUN = [
  '{"case_count":2,"cases":[',
  '{"attempt":1,"case_id":"smoke-001-capital","passed":true,"replayed":true,"score":1.0},',
  '{"attempt":2,"case_id":"smoke-003-json-only","passed":false,"replayed":false,"score":0.25}],',
  `"eval_run_id":"${MEASURED_ID}","fresh":true,"label":"baseline","model":"fake-1",`,
  '"pass_count":1,"provider":"fake","resumed":false,"samples":1,"score":0.625,"scored":1,',
  '"suite":"smoke"}',
].join("");

/**
 * The gateway's own refusal of a limit it will not serve. `/eval/runs` shares `/v1/runs`'s
 * `LimitError` contract and no recording holds the eval half of it, so the body is written here
 * in the shape the emitter sends rather than a recorded one being edited (MUST rule 24).
 */
const LIMIT_REFUSED = [
  '{"error":"LimitError",',
  "\"message\":\"limit 'nought' is not a positive integer.\"}",
].join("");

// ---- One answer per route, so nothing here is a network ----------------------------------------

type Answer = { status: number; text: string; headers?: Record<string, string> };

/** A response object with only what `fetch-panel` reads. */
function answer({ status, text, headers = {} }: Answer): Response {
  return {
    status,
    ok: status === 200,
    headers: { get: (name: string) => headers[name.toLowerCase()] ?? null },
    text: async () => text,
  } as unknown as Response;
}


type Route = Answer | "rejects" | "rejects silently";

/** Stub `fetch` with one answer per gateway route, and hand back the spy so a test can read the URLs. */
function answersWith(routes: Record<string, Route>): ReturnType<typeof vi.fn> {
  const spy = vi.fn(async (url: string) => {
    const next = routes[url];
    if (next === undefined) throw new Error(`no answer stubbed for ${url}`);
    if (next === "rejects") throw new Error("ECONNREFUSED");
    // A browser that refuses a fetch without a message: the class name is all there is to report.
    if (next === "rejects silently") throw new TypeError("");
    return answer(next);
  });
  vi.stubGlobal("fetch", spy);
  return spy;
}

/** The recorded `/health` the shell's pill polls, so no test here is about the pill. */
const HEALTH_URL = "/v1/health";
const HEALTH_OK: Answer = { status: health.code, text: health.text };

/** The proxy URL a gateway route is read through. */
function proxied(route: string): string {
  return route;
}

/** The listing route for a limit, as the panel spells it. */
function listingRoute(limit: string): string {
  return `/v1/eval/runs?limit=${limit}`;
}

/** The route one eval run's detail is read from. */
function runRoute(id: string): string {
  return `/v1/eval/runs/${id}`;
}

/** Let React settle a read. Every state change after this is a render, never a request. */
async function settle(): Promise<void> {
  await act(async () => {});
}

/** The shell as `app/layout.tsx` mounts it, around whatever a page renders. */
function shell(children: ReactNode) {
  return <AppShell>{children}</AppShell>;
}

/** `/evals` at a limit, rendered as the server renders it. */
async function renderEvals(limit?: string) {
    const page = pageAt(EvalRunsView, limit === undefined ? {} : { limit });
  const rendered = render(shell(page));
  await settle();
  return rendered;
}

/** `/evals?id=`, rendered as the server renders it (FR-64). */
async function renderDetail(evalRunId: string) {
  const page = pageAt(EvalRunsView, { id: evalRunId });
  const rendered = render(shell(page));
  await settle();
  return rendered;
}

/** The panel whose heading is `title`, so one page's panels never answer for each other. */
function panel(title: string): HTMLElement {
  return screen.getByRole("region", { name: title });
}

/**
 * The figure at a route and path, found by its provenance pair rather than by its `title`:
 * `SourceChip` carries the same `route -> path` title, and only the figure carries `data-path`.
 */
function figureAt(scope: HTMLElement, route: string, path: string): HTMLElement {
  const found = queryFigureAt(scope, route, path);
  if (found === null) throw new Error(`no figure for ${route} -> ${path}`);
  return found;
}

function queryFigureAt(scope: HTMLElement, route: string, path: string): HTMLElement | null {
  return scope.querySelector<HTMLElement>(`[data-route="${route}"][data-path="${path}"]`);
}

/** A listing row's "started" and a promotion's "decided" read in words (tickets 270, 303); the exact stamp is the tooltip's. */
const isHumanStarted = (figure: { path: string }): boolean =>
  /^\.runs\[\d+\]\.started_at$/.test(figure.path) || /^\.promotions\[\d+\]\.decided_at$/.test(figure.path);

/** Every figure of a tree, checked against the bytes of the bodies it claims to come from. */
function faithful(container: ParentNode, bodies: Record<string, string>): void {
  const figures = renderedFigures(container);
  expect(figures.length).toBeGreaterThan(0);
  expect(figuresWithoutSource(container)).toEqual([]);
  expect(divergences(figures.filter((f) => !isHumanStarted(f)), new Map(Object.entries(bodies)))).toEqual([]);
}

/** The listing body, parsed once for the tests that compare the table against what was sent. */
type SentRun = {
  eval_run_id: string;
  suite: string;
  label: string;
  started_at: string;
  case_count: number;
  passes: number;
  score: number | null;
  samples: number;
};

function sentRuns(): SentRun[] {
  return (JSON.parse(listing.text) as { runs: SentRun[] }).runs;
}

beforeEach(() => {
  pushed.length = 0;
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

afterAll(() => {
  mkdirSync(dirname(EVIDENCE), { recursive: true });
  writeFileSync(EVIDENCE, `${log.join("\n")}\n`, "utf8");
});

// ---- `/evals`: the listing ----------------------------------------------------------------------

describe("/evals lists every eval run as sent, in the gateway's order (FR-56)", () => {
  // Slow by nature: renders the full recorded listing; 3.7 s on the operator's PC.
  it("shows the eight fields of every row, row for row, in the order they were sent", { timeout: 15_000 }, async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(listingRoute(DEFAULT_LIMIT))]: { status: listing.code, text: listing.text },
    });
    const { container } = await renderEvals();

    const table = within(panel("Eval runs")).getByRole("table");
    const headers = within(table)
      .getAllByRole("columnheader")
      .map((cell) => cell.textContent);
    expect(headers.slice(0, 8)).toEqual([
      "eval run id",
      "suite",
      "label",
      "started",
      "cases",
      "passes",
      "score",
      "samples",
    ]);

    const sent = sentRuns();
    const sentChars = sourceCharactersByPath(listing.text);
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(sent.length);
    expect(rows.length).toBeGreaterThan(10);

    const route = listingRoute(DEFAULT_LIMIT);
    for (const [index, row] of rows.entries()) {
      const expected = sent[index];
      if (expected === undefined) throw new Error("the listing lost a row");
      const id = within(row).getByRole("link");
      expect(id.textContent).toBe(expected.eval_run_id);
      expect(id.getAttribute("href")).toBe(evalRunHref(expected.eval_run_id));
      for (const field of ["suite", "label", "started_at", "case_count", "passes", "samples"]) {
        expect(figureAt(row as HTMLElement, route, `.runs[${index}].${field}`)).toBeTruthy();
      }
      // A measured score is its own characters - `1.0` is not `1` (decision 31); the run with
      // no cases is the next test. The oracle is the raw body, never a re-serialized parse.
      const score = figureAt(row as HTMLElement, route, `.runs[${index}].score`);
      expect(score.textContent).toBe(
        expected.score === null ? "unknown" : sentChars.get(`.runs[${index}].score`),
      );
    }

    faithful(container, { [route]: listing.text });
    mkdirSync(dirname(EVALS_HTML), { recursive: true });
    writeFileSync(EVALS_HTML, container.innerHTML, "utf8");
    log.push(`/evals: ${rows.length} rows, eight fields each, in the gateway's order`);
  });

  it("reads a run with no cases as unknown with `no evidence: 0 cases`, never as 0.0", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(listingRoute(DEFAULT_LIMIT))]: { status: listing.code, text: listing.text },
    });
    await renderEvals();

    const route = listingRoute(DEFAULT_LIMIT);
    const zero = sentRuns().findIndex((run) => run.eval_run_id === ZERO_CASE_ID);
    expect(zero).toBeGreaterThanOrEqual(0);
    expect(sentRuns()[zero]?.score).toBeNull();
    expect(sentRuns()[zero]?.case_count).toBe(0);

    const row = within(panel("Eval runs")).getAllByRole("row").slice(1)[zero] as HTMLElement;
    const score = figureAt(row, route, `.runs[${zero}].score`);
    expect(score.textContent).toBe("unknown");
    expect(row.textContent).toContain("no evidence: 0 cases");
    // The fail-closed convention the promoter uses never reaches the screen (FR-47).
    expect(score.textContent).not.toBe("0.0");
    expect(score.textContent).not.toBe("0");
    expect(nullReasonFor(route, `.runs[${zero}].score`)).toBe("no evidence: 0 cases");

    // And its neighbour, which was measured, still reads as the characters the ledger spelled.
    const measuredRow = within(panel("Eval runs")).getAllByRole("row").slice(1)[2] as HTMLElement;
    expect(figureAt(measuredRow, route, ".runs[2].score").textContent).toBe("0.829218");
    log.push("/evals: `score: null` is `unknown` with `no evidence: 0 cases`, never `0.0` (FR-47)");
  });

  it("offers 20, 50 and 100, keeps the choice in the URL and says what it is showing", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(listingRoute(DEFAULT_LIMIT))]: { status: listing.code, text: listing.text },
    });
    await renderEvals();

    expect([...LIMITS]).toEqual(["20", "50", "100"]);
    const control = screen.getByTestId("limit-control");
    const choices = within(control).getAllByRole("radio") as HTMLInputElement[];
    expect(choices.map((choice) => choice.value)).toEqual([...LIMITS]);
    expect((within(control).getByLabelText(DEFAULT_LIMIT) as HTMLInputElement).checked).toBe(true);

    expect(screen.getByTestId("evals-footer").textContent).toBe(footerLine(DEFAULT_LIMIT));
    expect(screen.getByTestId("evals-footer").textContent).toBe(
      "showing up to 20, newest first, as the gateway ordered them",
    );

    fireEvent.click(within(control).getByLabelText("100"));
    expect(pushed).toEqual(["/evals?limit=100"]);
    cleanup();

    const spy = answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(listingRoute("100"))]: { status: listing.code, text: listing.text },
    });
    await renderEvals("100");
    expect(spy.mock.calls.map((call) => call[0])).toContain(proxied(listingRoute("100")));
    expect(screen.getByTestId("evals-footer").textContent).toBe(
      "showing up to 100, newest first, as the gateway ordered them",
    );
    log.push("/evals: the 20/50/100 control is URL state; the footer names the cap and the order");
  });

  it("names the route and `harnessed eval run` where the eval ledger is empty", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(listingRoute(DEFAULT_LIMIT))]: {
        status: emptyListing.code,
        text: emptyListing.text,
      },
    });
    const { container } = await renderEvals();

    const body = panel("Eval runs").textContent ?? "";
    expect(body).toContain("No eval runs recorded yet.");
    expect(body).not.toContain(listingRoute(DEFAULT_LIMIT));
    expect(body).toContain("harnessed eval run");
    expect(within(panel("Eval runs")).queryByRole("table")).toBeNull();
    expect(figuresWithoutSource(container)).toEqual([]);
    log.push("/evals: zero rows say `No eval runs recorded yet.` and the command that makes some");
  });

  it("shows the gateway's own refusal when the URL asks for a limit it will not serve", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(listingRoute("nought"))]: { status: 400, text: LIMIT_REFUSED },
    });
    await renderEvals("nought");

    const body = panel("Eval runs").textContent ?? "";
    expect(body).toContain("LimitError");
    expect(body).toContain("limit 'nought' is not a positive integer.");
    log.push("/evals: a limit the gateway refuses is its own 400, quoted, never a client guess");
  });

  it("renders no figure from memory when the gateway cannot be reached", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(listingRoute(DEFAULT_LIMIT))]: "rejects",
    });
    const { container } = await renderEvals();

    expect(screen.getByRole("heading", { name: "Gateway unreachable" })).toBeTruthy();
    expect(container.querySelectorAll("[data-figure]")).toHaveLength(0);
    log.push("/evals: an unreachable gateway leaves 0 figures on the page (`D-6`)");
  });
});

// ---- Picking the two ids a comparison is of ------------------------------------------------------

describe("/evals opens a comparison at two ids taken verbatim from the response (FR-56)", () => {
  // Slow by nature: renders the listing and drives two picks; 3.7 s on the operator's PC.
  it("builds `/evals?before=&after=` from the two `eval_run_id` fields picked", { timeout: 15_000 }, async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(listingRoute(DEFAULT_LIMIT))]: { status: listing.code, text: listing.text },
    });
    await renderEvals();

    // With nothing picked there is no comparison to open, and the page says what it needs.
    expect(screen.queryByTestId("compare-link")).toBeNull();
    expect(screen.getByTestId("compare-prompt").textContent).toContain("before");

    const sent = sentRuns();
    const before = sent.findIndex((run) => run.eval_run_id === BEFORE_ID);
    const after = sent.findIndex((run) => run.eval_run_id === AFTER_ID);
    fireEvent.click(screen.getByRole("radio", { name: `before: ${BEFORE_ID}` }));
    expect(screen.queryByTestId("compare-link")).toBeNull();
    fireEvent.click(screen.getByRole("radio", { name: `after: ${AFTER_ID}` }));

    const link = screen.getByTestId("compare-link");
    expect(link.getAttribute("href")).toBe(
      `/evals?before=${BEFORE_ID}&after=${AFTER_ID}`,
    );
    // Both ids are the response's own characters at their own paths (MUST rule 25).
    const table = within(panel("Eval runs")).getByRole("table");
    const route = listingRoute(DEFAULT_LIMIT);
    expect(figureAt(table, route, `.runs[${before}].eval_run_id`).textContent).toBe(BEFORE_ID);
    expect(figureAt(table, route, `.runs[${after}].eval_run_id`).textContent).toBe(AFTER_ID);
    expect(compareHref(BEFORE_ID, AFTER_ID)).toBe(link.getAttribute("href"));

    // Re-picking replaces that side and leaves the other where it was: one pair, never a list.
    fireEvent.click(screen.getByRole("radio", { name: `before: ${MEASURED_ID}` }));
    expect(screen.getByTestId("compare-link").getAttribute("href")).toBe(
      `/evals?before=${MEASURED_ID}&after=${AFTER_ID}`,
    );
    log.push("/evals: the compare link is two `eval_run_id` fields verbatim, no id assembled");
  });

  it("never reorders or hides a row because of what was picked (`D-1`)", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(listingRoute(DEFAULT_LIMIT))]: { status: listing.code, text: listing.text },
    });
    await renderEvals();

    const ids = () =>
      within(within(panel("Eval runs")).getByRole("table"))
        .getAllByRole("link")
        .map((link) => link.textContent);
    const before = ids();
    expect(before).toEqual(sentRuns().map((run) => run.eval_run_id));

    fireEvent.click(screen.getByRole("radio", { name: `before: ${BEFORE_ID}` }));
    fireEvent.click(screen.getByRole("radio", { name: `after: ${AFTER_ID}` }));
    expect(ids()).toEqual(before);
    log.push("/evals: picking a pair sorts, filters and hides nothing (`D-1`)");
  });
});

// ---- `/evals?id=`: one run and its cases -------------------------------------------------

describe("/evals?id= shows the run's fields and every case as sent (FR-56)", () => {
  it("reads the run from /eval/runs/{id} and shows each case's score, verdict and attempt", async () => {
    const spy = answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(runRoute(MEASURED_ID))]: { status: measured.code, text: measured.text },
    });
    const { container } = await renderDetail(MEASURED_ID);

    expect(spy.mock.calls.map((call) => call[0])).toContain(proxied(runRoute(MEASURED_ID)));
    const run = panel("Eval run");
    const route = runRoute(MEASURED_ID);
    const sent = JSON.parse(measured.text) as {
      cases: { case_id: string; score: number; passed: boolean; attempt: number }[];
      eval_run_id: string;
      suite: string;
      label: string;
      case_count: number;
      pass_count: number;
      samples: number;
      score: number;
      scored: number;
    };

    for (const field of [
      "eval_run_id",
      "suite",
      "label",
      "case_count",
      "pass_count",
      "samples",
      "score",
      "scored",
    ]) {
      expect(figureAt(run, route, `.${field}`)).toBeTruthy();
    }
    const chars = sourceCharactersByPath(measured.text);
    expect(figureAt(run, route, ".eval_run_id").textContent).toBe(sent.eval_run_id);
    expect(figureAt(run, route, ".score").textContent).toBe(chars.get(".score"));
    expect(chars.get(".score")).toBe("0.8");

    const rows = within(within(run).getByRole("table")).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(sent.cases.length);
    expect(rows.length).toBeGreaterThan(1);
    for (const [index, row] of rows.entries()) {
      const expected = sent.cases[index];
      if (expected === undefined) throw new Error("the run lost a case");
      expect(figureAt(row as HTMLElement, route, `.cases[${index}].case_id`).textContent).toBe(
        expected.case_id,
      );
      // Characters, not a re-serialized parse: a case scored `1.0` never reads `1` (decision 31).
      expect(figureAt(row as HTMLElement, route, `.cases[${index}].score`).textContent).toBe(
        chars.get(`.cases[${index}].score`),
      );
      expect(figureAt(row as HTMLElement, route, `.cases[${index}].attempt`).textContent).toBe(
        chars.get(`.cases[${index}].attempt`),
      );
      const mark = within(row as HTMLElement).getByTestId("pass-mark-word");
      expect(mark.textContent).toBe(expected.passed ? "pass" : "fail");
    }

    faithful(container, { [route]: measured.text });
    mkdirSync(dirname(DETAIL_HTML), { recursive: true });
    writeFileSync(DETAIL_HTML, container.innerHTML, "utf8");
    log.push(
      `/evals?id=: ${rows.length} cases, each with score, verdict and attempt as sent`,
    );
  });

  it("tags a case the ledger replayed, and leaves the one it scored fresh untagged", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(runRoute(MEASURED_ID))]: { status: 200, text: MIXED_RUN },
    });
    const { container } = await renderDetail(MEASURED_ID);

    const route = runRoute(MEASURED_ID);
    const rows = within(within(panel("Eval run")).getByRole("table"))
      .getAllByRole("row")
      .slice(1);
    expect(rows).toHaveLength(2);
    expect(REPLAYED_TAG).toBe("replayed from ledger");
    expect(rows[0]?.textContent).toContain(REPLAYED_TAG);
    expect(rows[1]?.textContent).not.toContain(REPLAYED_TAG);
    // The field itself is still a figure of this response, at its own path, either way.
    expect(figureAt(rows[0] as HTMLElement, route, ".cases[0].replayed").textContent).toBe("true");
    expect(figureAt(rows[1] as HTMLElement, route, ".cases[1].replayed").textContent).toBe("false");

    faithful(container, { [route]: MIXED_RUN });
    log.push("/evals?id=: `replayed: true` is tagged; a fresh case carries no tag");
  });

  it("shows `resumed` and `fresh` as the response sent them, on both of their values", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(runRoute(MEASURED_ID))]: { status: measured.code, text: measured.text },
    });
    await renderDetail(MEASURED_ID);

    const run = panel("Eval run");
    const route = runRoute(MEASURED_ID);
    expect(figureAt(run, route, ".resumed").textContent).toBe("true");
    expect(figureAt(run, route, ".fresh").textContent).toBe("false");
    // Neither is a verdict, so neither is drawn as a pass or a fail (`PassMark`'s own rule).
    expect(run.textContent).toContain("resumed: ");
    expect(run.textContent).toContain("fresh: ");
    cleanup();

    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(runRoute(MEASURED_ID))]: { status: 200, text: MIXED_RUN },
    });
    await renderDetail(MEASURED_ID);
    expect(figureAt(panel("Eval run"), route, ".resumed").textContent).toBe("false");
    expect(figureAt(panel("Eval run"), route, ".fresh").textContent).toBe("true");
    log.push("/evals?id=: `resumed` and `fresh` are shown as sent, never as a verdict");
  });

  it("reads a run with no cases as unknown, and says so instead of showing an empty table", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(runRoute(ZERO_CASE_ID))]: { status: zeroCase.code, text: zeroCase.text },
    });
    const { container } = await renderDetail(ZERO_CASE_ID);

    const route = runRoute(ZERO_CASE_ID);
    const run = panel("Eval run");
    const reason = nullReasonFor(route, ".score");
    expect(reason).toBe("no evidence: 0 cases");
    const score = figureAt(run, route, ".score");
    expect(score.textContent).toBe("unknown");
    expect(run.textContent).toContain(reason);
    expect(score.textContent).not.toBe("0.0");
    expect(figureAt(run, route, ".case_count").textContent).toBe("0");

    // The run's own fields are still readable: an empty `cases[]` is not an empty panel.
    expect(figureAt(run, route, ".eval_run_id").textContent).toBe(ZERO_CASE_ID);
    expect(within(run).queryByRole("table")).toBeNull();
    expect(within(run).getByTestId("cases-empty").textContent).toBe(NO_CASES);

    faithful(container, { [route]: zeroCase.text });
    mkdirSync(dirname(ZERO_CASE_HTML), { recursive: true });
    writeFileSync(ZERO_CASE_HTML, container.innerHTML, "utf8");
    log.push("/evals?id=: 0 cases is an explained `unknown` score, not a `0.0` (FR-47)");
  });

  it("shows the gateway's own 404 body where the id is one the ledgers never held", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(runRoute(UNKNOWN_ID))]: { status: noSuchRun.code, text: noSuchRun.text },
    });
    const { container } = await renderDetail(UNKNOWN_ID);

    expect(screen.getAllByRole("alert").length).toBeGreaterThan(0);
    const body = panel("Eval run").textContent ?? "";
    expect(body).toContain("SurfaceNotFound");
    expect(body).toContain(
      "no eval run 'eval_00000000000000000000000000': the ledgers hold nothing under that id.",
    );
    expect(container.querySelectorAll("[data-figure]")).toHaveLength(0);
    log.push("/evals?id=: an unheld id is `AbsentState`, quoting the gateway's own body");
  });
});

// ---- Decision 40's one line, and the page walk both pages owe FR-51 -------------------------------

describe("both pages render the served provider and no sourceless figure (FR-72, FR-51)", () => {
  it("shows provider: fake in mono from the served field, glossed with the Vision's caveat", async () => {
    expect(FAKE_GLOSS).toBe("this proves the promotion mechanism, not model quality");
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(runRoute(MEASURED_ID))]: { status: measured.code, text: measured.text },
    });
    await renderDetail(MEASURED_ID);

    const line = screen.getByTestId("provider-line");
    const provider = figureIn(line, ".provider");
    expect(provider.textContent).toBe("fake");
    // A figure, not copy: it names the route and path it was read from, and it is the mono
    // `Value` figure rather than a sentence (`D-4`).
    expect(provider.getAttribute("data-route")).toBe(runRoute(MEASURED_ID));
    expect(provider.hasAttribute("data-figure")).toBe(true);
    expect(figureIn(line, ".model").textContent).toBe(servedText(measured.text, "model"));
    expect(within(line).getByTestId("provider-gloss").textContent).toBe(FAKE_GLOSS);
    expect(line.textContent).toContain("provider:");
    log.push("/evals?id=: provider: fake from the served field, with the Vision's caveat as gloss");
  });

  it("reads unknown with its migration where the run predates 010, never a blank", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(runRoute(ZERO_CASE_ID))]: { status: zeroCase.code, text: zeroCase.text },
    });
    await renderDetail(ZERO_CASE_ID);

    const line = screen.getByTestId("provider-line");
    for (const path of [".provider", ".model"]) {
      expect(figureIn(line, path).textContent).toBe("unknown");
      expect(nullReasonFor(runRoute(ZERO_CASE_ID), path)).toBe("recorded before Harnessed recorded the provider and model");
    }
    expect(line.textContent).toContain("recorded before Harnessed recorded the provider and model");
    expect(within(line).queryByTestId("provider-gloss")).toBeNull();
    log.push("/evals?id=: a pre-010 run reads unknown, recorded before migration 010");
  });

  it("lists every run's provider and model as sent, unknown for a pre-010 row", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(listingRoute(DEFAULT_LIMIT))]: { status: listing.code, text: listing.text },
    });
    const { container } = await renderEvals();
    const rows = (JSON.parse(listing.text) as { runs: { provider: string | null }[] }).runs;
    expect(rows.some((row) => row.provider === "fake")).toBe(true);
    expect(rows.some((row) => row.provider === null)).toBe(true);
    rows.forEach((row, index) => {
      const cell = container.querySelector(`[data-figure][data-path=".runs[${index}].provider"]`);
      expect(cell?.textContent).toBe(row.provider ?? "unknown");
    });
    expect(nullReasonFor(listingRoute(DEFAULT_LIMIT), ".runs[0].provider")).toBe(
      "recorded before Harnessed recorded the provider and model",
    );
    log.push("/evals: provider and model columns, each row as the gateway sent it");
  });

  it("drops decision 40's static line from both pages", async () => {
    expect(existsSync(resolve(DASHBOARD, "app/evals/caveat.ts"))).toBe(false);
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(listingRoute(DEFAULT_LIMIT))]: { status: listing.code, text: listing.text },
      [proxied(runRoute(MEASURED_ID))]: { status: measured.code, text: measured.text },
    });
    await renderEvals();
    expect(screen.queryByTestId("provider-caveat")).toBeNull();
    expect(document.body.textContent).not.toContain("does not record which provider");
    cleanup();

    await renderDetail(MEASURED_ID);
    expect(screen.queryByTestId("provider-caveat")).toBeNull();
    expect(document.body.textContent).not.toContain("does not record which provider");
    log.push("/evals and /evals?id=: decision 40's static copy is gone");
  });

  it("leaves 0 figures without a source and 0 characters that differ from the wire", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(listingRoute(DEFAULT_LIMIT))]: { status: listing.code, text: listing.text },
    });
    const listed = await renderEvals();
    const onList = renderedFigures(listed.container);
    expect(figuresWithoutSource(listed.container)).toEqual([]);
    expect(divergences(onList.filter((f) => !isHumanStarted(f)), new Map([[listingRoute(DEFAULT_LIMIT), listing.text]]))).toEqual([]);
    cleanup();

    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(runRoute(MEASURED_ID))]: { status: measured.code, text: measured.text },
    });
    const detail = await renderDetail(MEASURED_ID);
    const onDetail = renderedFigures(detail.container);
    expect(figuresWithoutSource(detail.container)).toEqual([]);
    expect(divergences(onDetail, new Map([[runRoute(MEASURED_ID), measured.text]]))).toEqual([]);

    expect(onList.length).toBeGreaterThan(80);
    expect(onDetail.length).toBeGreaterThan(20);
    log.push(
      `walk: ${onList.length} figures on /evals and ${onDetail.length} on /evals?id=, 0 unsourced`,
    );
  });

  it("opens the response a figure came from, from the figure itself, and closes on Esc", async () => {
    vi.stubGlobal("matchMedia", (query: string) => ({ matches: false, media: query }));
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(runRoute(MEASURED_ID))]: { status: measured.code, text: measured.text },
    });
    await renderDetail(MEASURED_ID);

    const route = runRoute(MEASURED_ID);
    const trigger = figureAt(panel("Eval run"), route, ".eval_run_id");
    fireEvent.keyDown(trigger, { key: "Enter" });
    const dialog = screen.getByRole("dialog");

    // Labelled "Where this came from" - never the route (decision 68, `D-9`).
    expect(within(dialog).getByRole("heading").textContent).toBe("Where this came from");
    expect(dialog.textContent).not.toContain(route);
    expect(dialog.textContent).not.toContain("GET");
    expect(screen.queryByTestId("curl")).toBeNull();
    expect(within(dialog).getByTestId("cli-twin").textContent).toBe(
      "harnessed eval show <id> --json",
    );
    expect(dialog.getAttribute("data-motion")).toBe("slide");

    expect(within(dialog).getByTestId("record-view")).toBeTruthy();
    expect(within(dialog).getByTestId("cited").textContent).toBe(MEASURED_ID);

    const controls = within(dialog).getAllByRole("button");
    const first = controls.at(0) as HTMLElement;
    const last = controls.at(-1) as HTMLElement;
    last.focus();
    fireEvent.keyDown(dialog, { key: "Tab" });
    expect(document.activeElement).toBe(first);
    fireEvent.keyDown(dialog, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(last);
    fireEvent.keyDown(dialog, { key: "ArrowDown" });

    const written: string[] = [];
    vi.stubGlobal("navigator", {
      clipboard: {
        writeText: async (line: string) => {
          written.push(line);
        },
      },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Copy" }));
    expect(written).toEqual(["harnessed eval show <id> --json"]);

    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(trigger);
    log.push("/evals?id=: a figure opens the drawer it was read out of, with its CLI twin");
  });

  it("opens the listing's own source, with the listing's `--json` twin", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(listingRoute(DEFAULT_LIMIT))]: { status: listing.code, text: listing.text },
    });
    await renderEvals();

    const route = listingRoute(DEFAULT_LIMIT);
    const trigger = figureAt(panel("Eval runs"), route, ".runs[0].suite");
    // Enter opens a figure; nothing else does, so it is not a trap for any other key.
    fireEvent.keyDown(trigger, { key: "ArrowRight" });
    expect(screen.queryByRole("dialog")).toBeNull();
    fireEvent.click(trigger);
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByTestId("cli-twin").textContent).toBe("harnessed eval list --json");
    expect(screen.queryByTestId("curl")).toBeNull();
    expect(dialog.textContent).not.toContain(route);
    // No motion setting to read is an operator who has not asked for motion (design.md).
    expect(dialog.getAttribute("data-motion")).toBe("instant");
    fireEvent.click(within(dialog).getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    log.push("/evals: the listing's drawer names `harnessed eval list --json`, no endpoint string");
  });
});

// ---- The seven states design.md lets no panel skip ------------------------------------------------

describe("both panels wear every state, and remember nothing between them (design.md)", () => {
  it("is busy before an answer and holds the answer after it", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(runRoute(MEASURED_ID))]: { status: measured.code, text: measured.text },
    });
    const page = pageAt(EvalRunsView, { id: MEASURED_ID });
    render(shell(page));

    expect(panel("Eval run").getAttribute("aria-busy")).toBe("true");
    expect(screen.getAllByTestId("skeleton-row").length).toBeGreaterThan(0);

    await settle();
    expect(panel("Eval run").getAttribute("aria-busy")).toBe("false");
    expect(screen.queryAllByTestId("skeleton-row")).toEqual([]);
    log.push("panels: `loading` is the busy skeleton, and the answer replaces it");
  });

  it("says which route, status and byte count it is showing, on the dashboard's own clock", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(listingRoute(DEFAULT_LIMIT))]: { status: listing.code, text: listing.text },
    });
    await renderEvals();

    const footer = panel("Eval runs").querySelector("footer");
    expect(footer?.textContent).toContain("Updated 0 min ago");
    expect(footer?.textContent).not.toContain(listingRoute(DEFAULT_LIMIT));
    expect(footer?.textContent).not.toContain("200");
    expect(footer?.textContent).not.toContain("bytes");
    expect(footer?.textContent).not.toContain("(dashboard clock)");
    log.push("panels: the footer says only how fresh it is, no route, status or bytes (D-9)");
  });

  // Slow by nature: renders the page twice across a five-minute fake-clock jump; 4.0 s on the operator's PC.
  it("goes stale five minutes after it fetched, and a refetch clears the badge", { timeout: 15_000 }, async () => {
    vi.useFakeTimers();
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(listingRoute(DEFAULT_LIMIT))]: { status: listing.code, text: listing.text },
    });
    const page = pageAt(EvalRunsView, {});
    render(shell(page));

    // Settle the read first: the elapsed time this test means is time after the answer arrived.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(screen.getByTestId("stale-badge").textContent).toBe("Updated 0 min ago");
    expect(screen.getByTestId("stale-badge").getAttribute("data-warn")).toBe("false");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5 * 60 * 1000);
    });
    expect(screen.getByTestId("stale-badge").textContent).toBe("Updated 5 min ago");
    expect(screen.getByTestId("stale-badge").getAttribute("data-warn")).toBe("true");
    expect(screen.getByTestId("panel-announcement").textContent).toBe("Updated 5 min ago");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60 * 1000);
    });
    expect(screen.getByTestId("stale-badge").textContent).toBe("Updated 6 min ago");
    expect(screen.getByTestId("panel-announcement").textContent).toBe("Updated 5 min ago");

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(screen.getByTestId("stale-badge").textContent).toBe("Updated 0 min ago");
    expect(screen.getByTestId("stale-badge").getAttribute("data-warn")).toBe("false");
    expect(screen.getByTestId("panel-announcement").textContent).toBe("");
    log.push("panels: `Updated N min ago` at five minutes, announced once, cleared by `Refresh`");
  });

  it("is unreachable where the browser's own fetch is refused, not only where a header says so", async () => {
    answersWith({ [HEALTH_URL]: HEALTH_OK, [proxied(listingRoute(DEFAULT_LIMIT))]: "rejects" });
    const { container } = await renderEvals();

    expect(screen.getByRole("heading", { name: "Gateway unreachable" })).toBeTruthy();
    expect(screen.getByTestId("unreachable-cause").textContent).toBe("ECONNREFUSED");
    expect(container.querySelectorAll("[data-figure]")).toHaveLength(0);
    log.push("panels: a refused browser fetch is `unreachable` too, with the cause it gave");
  });

  it("names the cause a refused read gave, and invents none where it gave nothing", async () => {
    // A rejection with no message of its own is reported by its class, never as a blank: an
    // `unreachable` that said "the request failed with ." would be reporting a guess as a fact.
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(listingRoute(DEFAULT_LIMIT))]: "rejects silently",
    });
    await renderEvals();
    expect(screen.getByTestId("unreachable-cause").textContent).toBe("TypeError");
    cleanup();

    log.push("panels: a refused read names its own cause, and names none where there was none");
  });

  it("quotes a body that is not the error contract as itself, and offers the read again", async () => {
    const spy = answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(listingRoute(DEFAULT_LIMIT))]: { status: 500, text: "the gateway fell over" },
    });
    await renderEvals();

    const body = panel("Eval runs").textContent ?? "";
    expect(body).toContain("UnreadableBody");
    expect(body).toContain("the gateway fell over");

    const before = spy.mock.calls.length;
    await act(async () => {
      fireEvent.click(
        within(panel("Eval runs")).getAllByRole("button", { name: "Refetch" }).at(0) as HTMLElement,
      );
    });
    expect(spy.mock.calls.length).toBeGreaterThan(before);
    log.push("panels: a body outside decision 24's contract is quoted as itself, and read again");
  });
});

// ---- What the shell says while an evals panel reads -----------------------------------------------

describe("the pill speaks for its own poll, and the panel for its own read (decision 34)", () => {
  it("says the gateway answers while the listing renders", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(listingRoute(DEFAULT_LIMIT))]: { status: listing.code, text: listing.text },
    });
    await renderEvals();

    const pill = screen.getByTestId("gateway-status-pill");
    expect(pill.getAttribute("data-state")).toBe("reachable");
    expect(pill.textContent).toContain("gateway reachable");
    expect(screen.getByTestId("pill-announcement").textContent).toBe("gateway reachable");

    fireEvent.click(pill);
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByTestId("record-view")).toBeTruthy();
    expect(within(dialog).queryByTestId("cited")).toBeNull();

    // And closing it hands the keyboard back to the pill, which is the only control that opened it.
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(screen.getByTestId("gateway-status-pill"));
    log.push("/evals: the pill reads `gateway reachable`, opens its `/health` and gives focus back");
  });

  it("keeps saying what its own poll found when a data route is the thing that failed", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(listingRoute(DEFAULT_LIMIT))]: "rejects",
    });
    await renderEvals();

    expect(screen.getByRole("heading", { name: "Gateway unreachable" })).toBeTruthy();
    expect(screen.getByTestId("gateway-status-pill").getAttribute("data-state")).toBe("reachable");
    log.push("/evals: a failed data read never rewrites the pill, which reads its own poll");
  });

  it("says unreachable, and then error, when it is the poll that fails", async () => {
    answersWith({
      [HEALTH_URL]: "rejects",
      [proxied(listingRoute(DEFAULT_LIMIT))]: { status: listing.code, text: listing.text },
    });
    await renderEvals();

    const pill = screen.getByTestId("gateway-status-pill");
    expect(pill.getAttribute("data-state")).toBe("unreachable");
    expect(pill.textContent).toContain("gateway unreachable");
    expect((pill as HTMLButtonElement).disabled).toBe(true);
    cleanup();

    answersWith({
      [HEALTH_URL]: { status: 500, text: '{"error":"InternalError","message":"it failed"}' },
      [proxied(listingRoute(DEFAULT_LIMIT))]: { status: listing.code, text: listing.text },
    });
    await renderEvals();
    const failed = screen.getByTestId("gateway-status-pill");
    expect(failed.getAttribute("data-state")).toBe("error");
    expect(failed.textContent).toContain("gateway error 500");
    log.push("/evals: the pill's four words are its own poll's, never a panel's");
  });
});

// ---- The shell these two pages are served inside --------------------------------------------------

describe("the pages name the origin they were served from", () => {
  it("puts that origin in the line an operator copies", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(runRoute(MEASURED_ID))]: { status: measured.code, text: measured.text },
    });
    const page = pageAt(EvalRunsView, { id: MEASURED_ID });
    render(
      <AppShell>{page}</AppShell>,
    );
    await settle();

    const route = runRoute(MEASURED_ID);
    const trigger = figureAt(panel("Eval run"), route, ".eval_run_id");
    fireEvent.click(trigger);
    const dialog = screen.getByRole("dialog");
    // No endpoint string anywhere - not the gateway's relative origin, not a `curl` line
    // (decision 68, `D-9`). Raw access is the CLI `--json` twin instead.
    expect(dialog.textContent).not.toContain(window.location.origin);
    expect(dialog.textContent).not.toContain(GATEWAY);
    expect(screen.queryByTestId("curl")).toBeNull();
    expect(within(dialog).getByTestId("cli-twin").textContent).toBe(
      "harnessed eval show <id> --json",
    );
    log.push("/evals?id=: the drawer offers the CLI twin, not a gateway URL (D-9)");
  });

  it("is navigable where the sidebar has become a menu button", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(listingRoute(DEFAULT_LIMIT))]: { status: listing.code, text: listing.text },
    });
    await renderEvals();

    const menu = screen.getByRole("button", { name: "Menu" });
    expect(screen.getByRole("navigation").getAttribute("data-open")).toBe("false");
    fireEvent.click(menu);
    expect(screen.getByRole("navigation").getAttribute("data-open")).toBe("true");
    expect(
      within(screen.getByRole("navigation")).getByRole("link", { name: "Evals" }),
    ).toBeTruthy();
    log.push("/evals: the page is reachable from the narrow menu as well as from the sidebar");
  });
});

// ---- Evidence ------------------------------------------------------------------------------------

describe("evidence", () => {
  it("writes the manifest this ticket declares", () => {
    expect(log.length).toBeGreaterThan(10);
    mkdirSync(dirname(EVIDENCE), { recursive: true });
    writeFileSync(EVIDENCE, `${log.join("\n")}\n`, "utf8");
  });
});

/** The one rendered figure at a jq path inside `scope`. */
function figureIn(scope: HTMLElement, path: string): HTMLElement {
  const found = scope.querySelector<HTMLElement>(`[data-figure][data-path="${path}"]`);
  if (found === null) throw new Error(`no figure at ${path}`);
  return found;
}

/** A top-level string field as the recorded body spelled it. */
function servedText(body: string, key: string): string {
  return String((JSON.parse(body) as Record<string, unknown>)[key]);
}

});

describe("compare (ticket 108)", () => {
const DASHBOARD = resolve(import.meta.dirname, "../..");
const REPO = resolve(DASHBOARD, "..");
const GATEWAY_FIXTURES = resolve(DASHBOARD, "tests/fixtures/gateway");
const EVIDENCE = resolve(REPO, "evidence/108/compare.txt");
const COMPARE_HTML = resolve(REPO, "evidence/108/compare.html");
const MISMATCH_HTML = resolve(REPO, "evidence/108/suite-mismatch.html");
const ZERO_SIDE_HTML = resolve(REPO, "evidence/108/zero-side.html");

/** The gateway this test's shell is pointed at. Not the operator's 8756 (decision 30). */
const GATEWAY = GATEWAY_ORIGIN;

/** The lines this run writes to `evidence/108/compare.txt`. */
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

const regression = recorded("eval.compare.regression");
const zeroSide = recorded("eval.compare.zero-case-side");
const suiteMismatch = recorded("badrequest.eval.compare.suite-mismatch");
const missingParameter = recorded("badrequest.eval.compare.missing-parameter");
const unknownBefore = recorded("notfound.eval.compare.unknown-before");
const unknownAfter = recorded("notfound.eval.compare.unknown-after");
const health = recorded("health");

/** The ids the recordings hold, and the id none of them does. */
const BEFORE_ID = "eval_recorded_before";
const AFTER_ID = "eval_recorded_after";
const ZERO_ID = "eval_recorded_zero";
const OTHER_SUITE_ID = "eval_recorded_other_suite";
const UNKNOWN_ID = "eval_00000000000000000000000000";

/** The four lists FR-62 serves, in the order it serves them. */
const LISTS = ["regressions", "soft_drops", "new_cases", "missing_cases"];

// ---- One answer per route, so nothing here is a network ----------------------------------------

type Answer = { status: number; text: string; headers?: Record<string, string> };

/** A response object with only what `fetch-panel` reads. */
function answer({ status, text, headers = {} }: Answer): Response {
  return {
    status,
    ok: status === 200,
    headers: { get: (name: string) => headers[name.toLowerCase()] ?? null },
    text: async () => text,
  } as unknown as Response;
}


type Route = Answer | "rejects" | "rejects silently" | "rejects with no error";

/** Stub `fetch` with one answer per gateway route, and hand back the spy so a test can read the URLs. */
function answersWith(routes: Record<string, Route>): ReturnType<typeof vi.fn> {
  const spy = vi.fn(async (url: string) => {
    const next = routes[url];
    if (next === undefined) throw new Error(`no answer stubbed for ${url}`);
    if (next === "rejects") throw new Error("ECONNREFUSED");
    // A browser that refuses a fetch without a message: the class name is all there is to report.
    if (next === "rejects silently") throw new TypeError("");
    // And a refusal that is not an `Error` at all: reported as itself, never as an invented code.
    if (next === "rejects with no error") throw "the browser refused the request";
    return answer(next);
  });
  vi.stubGlobal("fetch", spy);
  return spy;
}

/** The recorded `/health` the shell's pill polls, so no test here is about the pill. */
const HEALTH_URL = "/v1/health";
const HEALTH_OK: Answer = { status: health.code, text: health.text };

/** The proxy URL a gateway route is read through. */
function proxied(route: string): string {
  return route;
}

/** Let React settle a read. Every state change after this is a render, never a request. */
async function settle(): Promise<void> {
  await act(async () => {});
}

/** The shell as `app/layout.tsx` mounts it, around whatever a page renders. */
function shell(children: ReactNode) {
  return <AppShell>{children}</AppShell>;
}

type Params = { before?: string; after?: string };

/** `/evals/compare` at whatever the URL carries, rendered as the server renders it. */
async function renderCompare(params: Params) {
  const page = pageAt(CompareView, params);
  const rendered = render(shell(page));
  await settle();
  return rendered;
}

/** The comparison panel, so the shell's own regions never answer for it. */
function panel(): HTMLElement {
  return screen.getByRole("region", { name: COMPARE_TITLE });
}

/**
 * The figure at a route and path, found by its provenance pair rather than by its `title`:
 * `SourceChip` carries the same `route -> path` title, and only the figure carries `data-path`.
 */
function figureAt(scope: HTMLElement, route: string, path: string): HTMLElement {
  const found = scope.querySelector<HTMLElement>(`[data-route="${route}"][data-path="${path}"]`);
  if (found === null) throw new Error(`no figure for ${route} -> ${path}`);
  return found;
}

/** Every figure of a tree, checked against the bytes of the bodies it claims to come from. */
function faithful(container: ParentNode, bodies: Record<string, string>): void {
  const figures = renderedFigures(container);
  expect(figures.length).toBeGreaterThan(0);
  expect(figuresWithoutSource(container)).toEqual([]);
  expect(divergences(figures, new Map(Object.entries(bodies)))).toEqual([]);
}

/** The recorded comparison, parsed once for the tests that check the page against what was sent. */
type SentSide = {
  eval_run_id: string;
  suite: string;
  label: string;
  case_count: number;
  pass_count: number;
  samples: number;
  score: number | null;
};

type SentDelta = { case_id: string; score_before: number; score_after: number };

type SentComparison = {
  before: SentSide;
  after: SentSide;
  regressions: SentDelta[];
  soft_drops: SentDelta[];
  new_cases: string[];
  missing_cases: string[];
};

function sent(text: string): SentComparison {
  return JSON.parse(text) as SentComparison;
}

/** The routes the recorded comparisons answered, as this page spells them. */
const REGRESSION_ROUTE = compareRoute(BEFORE_ID, AFTER_ID);
const ZERO_SIDE_ROUTE = compareRoute(ZERO_ID, AFTER_ID);

beforeEach(() => {
  pushed.length = 0;
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

afterAll(() => {
  mkdirSync(dirname(EVIDENCE), { recursive: true });
  writeFileSync(EVIDENCE, `${log.join("\n")}\n`, "utf8");
});

// ---- One request, to the compare route, with both ids as typed ---------------------------------

describe("/evals/compare reads one route, the compare route (FR-56, FR-62)", () => {
  it("spells the route the recorded response answered, and asks no run route at all", async () => {
    const spy = answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(REGRESSION_ROUTE)]: { status: regression.code, text: regression.text },
    });
    await renderCompare({ before: BEFORE_ID, after: AFTER_ID });

    // The route this page builds is the route the gateway recorded itself answering.
    expect(REGRESSION_ROUTE).toBe(regression.route);
    expect(COMPARE_ROUTE).toBe("/v1/eval/compare");

    const asked = spy.mock.calls.map((call) => call[0] as string);
    const data = asked.filter((url) => url !== HEALTH_URL);
    expect(data).toEqual([proxied(REGRESSION_ROUTE)]);
    // Nothing here reads one eval run's own route: a side's figures are the comparison's own
    // leaves, not a second response merged into this panel (MUST rule 21).
    expect(asked.some((url) => url.includes("/v1/eval/runs"))).toBe(false);
    log.push("/evals/compare: one request, to the compare route, and none to /eval/runs/{id}");
  });

  it("carries both ids into the route exactly as the URL spelled them (MUST rule 25)", async () => {
    // An id with characters a client might be tempted to tidy: the route keeps every one of them.
    const odd = " Eval_Recorded_Before ";
    const route = compareRoute(odd, AFTER_ID);
    expect(route).toBe(`/v1/eval/compare?before=${odd}&after=${AFTER_ID}`);
    expect(compareHref(odd, AFTER_ID)).toBe(`/evals?before=${odd}&after=${AFTER_ID}`);

    const spy = answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(route)]: { status: unknownBefore.code, text: unknownBefore.text },
    });
    await renderCompare({ before: odd, after: AFTER_ID });
    expect(spy.mock.calls.map((call) => call[0])).toContain(proxied(route));
    log.push("/evals/compare: both ids reach the route as typed - no trim, no re-casing");
  });
});

// ---- The comparison itself ----------------------------------------------------------------------

describe("/evals/compare renders FR-62's response as served (FR-56)", () => {
  it("shows each side's score, pass count and case count, from the compare response", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(REGRESSION_ROUTE)]: { status: regression.code, text: regression.text },
    });
    const { container } = await renderCompare({ before: BEFORE_ID, after: AFTER_ID });

    const body = sent(regression.text);
    const chars = sourceCharactersByPath(regression.text);
    const scope = panel();
    expect([...SIDES]).toEqual(["before", "after"]);

    // The run-level score is one `ScorePair`: two read figures joined by a plain `->`.
    for (const side of SIDES) {
      expect(figureAt(scope, REGRESSION_ROUTE, `.${side}.score`).textContent).toBe(
        chars.get(`.${side}.score`),
      );
    }
    expect(
      within(scope).getByRole("group", { name: "score before 0.829218, score after 0.592181" }),
    ).toBeTruthy();

    // And the two sides are two rows, each one linking to that eval run's own page by its own id.
    const sides = within(scope).getAllByRole("table").at(0) as HTMLElement;
    const rows = within(sides).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(2);
    for (const [index, side] of SIDES.entries()) {
      const row = rows[index] as HTMLElement;
      expect(within(row).getByTestId(`side-${side}`).textContent).toBe(side);
      const link = within(row).getByRole("link");
      expect(link.textContent).toBe(body[side].eval_run_id);
      expect(link.getAttribute("href")).toBe(`/evals?id=${body[side].eval_run_id}`);
      for (const field of ["suite", "label", "pass_count", "case_count"]) {
        expect(figureAt(row, REGRESSION_ROUTE, `.${side}.${field}`).textContent).toBe(
          chars.get(`.${side}.${field}`),
        );
      }
    }

    // The characters, never a re-serialized parse: `0.829218` is not `0.83` (decision 31).
    expect(figureAt(scope, REGRESSION_ROUTE, ".before.score").textContent).toBe("0.829218");
    expect(figureAt(scope, REGRESSION_ROUTE, ".after.score").textContent).toBe("0.592181");
    expect(figureAt(scope, REGRESSION_ROUTE, ".before.pass_count").textContent).toBe("3");
    expect(figureAt(scope, REGRESSION_ROUTE, ".after.pass_count").textContent).toBe("2");

    faithful(container, { [REGRESSION_ROUTE]: regression.text });
    mkdirSync(dirname(COMPARE_HTML), { recursive: true });
    writeFileSync(COMPARE_HTML, container.innerHTML, "utf8");
    log.push("/evals/compare: each side's score, pass count and case count, as the comparison sent them");
  });

  it("lists regressions then soft drops as a ScorePair per case, in the order served", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(REGRESSION_ROUTE)]: { status: regression.code, text: regression.text },
    });
    await renderCompare({ before: BEFORE_ID, after: AFTER_ID });

    const body = sent(regression.text);
    const chars = sourceCharactersByPath(regression.text);

    for (const field of ["regressions", "soft_drops"] as const) {
      const scope = screen.getByTestId(`list-${field}`);
      const rows = within(within(scope).getByRole("table")).getAllByRole("row").slice(1);
      expect(rows).toHaveLength(body[field].length);
      expect(rows.length).toBeGreaterThan(0);
      for (const [index, row] of rows.entries()) {
        const expected = body[field][index];
        if (expected === undefined) throw new Error(`${field} lost a case`);
        const cell = row as HTMLElement;
        expect(figureAt(cell, REGRESSION_ROUTE, `.${field}[${index}].case_id`).textContent).toBe(
          expected.case_id,
        );
        for (const half of ["score_before", "score_after"]) {
          expect(figureAt(cell, REGRESSION_ROUTE, `.${field}[${index}].${half}`).textContent).toBe(
            chars.get(`.${field}[${index}].${half}`),
          );
        }
        // The join carries no direction: a hyphen and a greater-than, never an arrow glyph.
        expect(within(cell).getByTestId("score-pair-join").textContent).toBe("->");
      }
    }

    // `case-b` fell from `1.0` to `0.0`, and both are the body's own characters (decision 31).
    const first = screen.getByTestId("list-regressions");
    expect(figureAt(first, REGRESSION_ROUTE, ".regressions[0].score_before").textContent).toBe("1.0");
    expect(figureAt(first, REGRESSION_ROUTE, ".regressions[0].score_after").textContent).toBe("0.0");
    expect(
      within(first).getByRole("group", { name: "score before 1.0, score after 0.0" }),
    ).toBeTruthy();
    log.push("/evals/compare: regressions and soft drops are one ScorePair per case, in the served order");
  });

  it("lists the new and the missing case ids, after the two lists of cases that moved", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(REGRESSION_ROUTE)]: { status: regression.code, text: regression.text },
    });
    const { container } = await renderCompare({ before: BEFORE_ID, after: AFTER_ID });

    const body = sent(regression.text);
    for (const field of ["new_cases", "missing_cases"] as const) {
      const scope = screen.getByTestId(`list-${field}`);
      const items = within(scope).getAllByRole("listitem");
      expect(items).toHaveLength(body[field].length);
      expect(items.length).toBeGreaterThan(0);
      for (const [index, id] of body[field].entries()) {
        expect(figureAt(scope, REGRESSION_ROUTE, `.${field}[${index}]`).textContent).toBe(id);
      }
    }

    // The four lists are in the order FR-62 serves them, and so is every row inside them.
    const order = [...container.querySelectorAll("[data-testid^='list-']")].map((node) =>
      (node.getAttribute("data-testid") ?? "").slice("list-".length),
    );
    expect(order).toEqual(LISTS);
    log.push("/evals/compare: new and missing case ids follow the two moved lists, in served order");
  });

  it("says the comparison lists only cases that moved, and never says `unchanged`", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(REGRESSION_ROUTE)]: { status: regression.code, text: regression.text },
    });
    const { container } = await renderCompare({ before: BEFORE_ID, after: AFTER_ID });

    expect(screen.getByTestId("compare-note").textContent).toBe(ONLY_MOVED);
    expect(ONLY_MOVED).toContain("moved");
    expect(ONLY_MOVED).toContain("new");
    expect(ONLY_MOVED).toContain("missing");

    // `case-a` and `case-b` moved, `case-c` went missing and `case-d` is new. The suite's other
    // cases are in no list at all, and the page never calls any case unchanged.
    const page = container.textContent ?? "";
    expect(page).not.toContain("unchanged");
    expect(page).not.toContain("no change");
    log.push("/evals/compare: the page says what the lists hold; no case is ever called `unchanged`");
  });
});

// ---- The negative claim: nothing on this page was computed --------------------------------------

describe("/evals/compare computes, sorts and subtracts nothing (`D-1`, MUST rules 1 and 21)", () => {
  it("cites the compare route and a path in its bytes for every figure on the page", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(REGRESSION_ROUTE)]: { status: regression.code, text: regression.text },
    });
    const { container } = await renderCompare({ before: BEFORE_ID, after: AFTER_ID });

    // Every figure the recorded comparison can put on this page, counted: eight per side (its id,
    // suite and label, then score, pass count and case count, then provider and model), three per
    // moved case (the case id and the two halves of its `ScorePair`) and one per new or missing
    // case id.
    const figures = renderedFigures(container);
    expect(figures).toHaveLength(24);
    expect(figuresWithoutSource(container)).toEqual([]);

    // Every figure names this page's one route, so none came from another panel's response.
    expect([...new Set(figures.map((figure) => figure.route))]).toEqual([REGRESSION_ROUTE]);

    // And every cited path is a path the raw bytes actually hold: a computed figure has none.
    const chars = sourceCharactersByPath(regression.text);
    expect(figures.filter((figure) => !chars.has(figure.path))).toEqual([]);
    expect(divergences(figures, new Map([[REGRESSION_ROUTE, regression.text]]))).toEqual([]);
    log.push(`/evals/compare: ${figures.length} figures, every one a leaf of the compare response`);
  });

  it("shows no difference figure and no glyph that would carry a direction", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(REGRESSION_ROUTE)]: { status: regression.code, text: regression.text },
    });
    const { container } = await renderCompare({ before: BEFORE_ID, after: AFTER_ID });

    // `ScoreDelta` sends a before and an after and no difference, so a difference on this page
    // would be one the browser invented. These are what the two moved cases subtract to.
    const page = container.textContent ?? "";
    for (const invented of ["-1.0", "-1", "-100%", "-0.111111", "delta", "Delta", "better", "worse"]) {
      expect(page).not.toContain(invented);
    }
    for (const glyph of ["↑", "↓", "▲", "▼", "→", "⇒"]) {
      expect(page).not.toContain(glyph);
    }

    // No figure claims a path the payload does not carry, and `ScoreDelta` carries no third field.
    const paths = new Set(renderedFigures(container).map((figure) => figure.path));
    for (const path of paths) expect(path).not.toContain("diff");
    expect(paths.has(".regressions[0].score_before")).toBe(true);
    expect(paths.has(".regressions[0].score_after")).toBe(true);
    log.push("/evals/compare: 0 difference figures, 0 direction glyphs - `ScorePair` joins with `->`");
  });
});

// ---- The recorded states of the compare route ---------------------------------------------------

describe("/evals/compare wears each recorded state of the compare route (FR-56)", () => {
  it("quotes the gateway's own 400 verbatim when the two eval runs are of different suites", async () => {
    const route = compareRoute(BEFORE_ID, OTHER_SUITE_ID);
    expect(route).toBe(suiteMismatch.route);
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(route)]: { status: suiteMismatch.code, text: suiteMismatch.text },
    });
    const { container } = await renderCompare({ before: BEFORE_ID, after: OTHER_SUITE_ID });

    const shown = panel().textContent ?? "";
    const body = JSON.parse(suiteMismatch.text) as { error: string; message: string };
    expect(body.error).toBe("SuiteMismatch");
    expect(shown).toContain(body.error);
    expect(shown).toContain(body.message);
    expect(shown).toContain("400");
    // Nothing was rendered from the refused comparison: there is no comparison to render.
    expect(container.querySelectorAll("[data-figure]")).toHaveLength(0);

    mkdirSync(dirname(MISMATCH_HTML), { recursive: true });
    writeFileSync(MISMATCH_HTML, container.innerHTML, "utf8");
    log.push("/evals/compare: a suite mismatch is the gateway's own 400 `SuiteMismatch`, quoted");
  });

  it("names the side an unknown id was on, on either side, from the gateway's own 404", async () => {
    const cases: [Params, Recorded][] = [
      [{ before: UNKNOWN_ID, after: AFTER_ID }, unknownBefore],
      [{ before: BEFORE_ID, after: UNKNOWN_ID }, unknownAfter],
    ];
    for (const [params, record] of cases) {
      const route = compareRoute(params.before ?? "", params.after ?? "");
      expect(route).toBe(record.route);
      answersWith({
        [HEALTH_URL]: HEALTH_OK,
        [proxied(route)]: { status: record.code, text: record.text },
      });
      const { container } = await renderCompare(params);

      const body = JSON.parse(record.text) as { error: string; message: string };
      expect(body.error).toBe("SurfaceNotFound");
      expect(screen.getAllByRole("alert").length).toBeGreaterThan(0);
      const shown = panel().textContent ?? "";
      expect(shown).toContain(body.error);
      expect(shown).toContain(body.message);
      expect(container.querySelectorAll("[data-figure]")).toHaveLength(0);
      cleanup();
    }

    // The message the operator reads names which side the id was on, which is the whole point.
    expect(unknownBefore.text).toContain("on the before side");
    expect(unknownAfter.text).toContain("on the after side");
    log.push("/evals/compare: an unheld id is `AbsentState`, and the gateway's body names the side");
  });

  it("reads a zero-case side as unknown with `no evidence: 0 cases`, never as 0.0", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(ZERO_SIDE_ROUTE)]: { status: zeroSide.code, text: zeroSide.text },
    });
    const { container } = await renderCompare({ before: ZERO_ID, after: AFTER_ID });

    expect(ZERO_SIDE_ROUTE).toBe(zeroSide.route);
    const body = sent(zeroSide.text);
    expect(body.before.score).toBeNull();
    expect(body.before.case_count).toBe(0);

    const reason = nullReasonFor(ZERO_SIDE_ROUTE, ".before.score");
    expect(reason).toBe("no evidence: 0 cases");
    const scope = panel();
    const score = figureAt(scope, ZERO_SIDE_ROUTE, ".before.score");
    expect(score.textContent).toBe("unknown");
    expect(scope.textContent).toContain(reason);
    // The promoter's fail-closed convention never reaches the screen (FR-47, decision 35).
    expect(score.textContent).not.toBe("0.0");
    expect(score.textContent).not.toBe("0");
    expect(figureAt(scope, ZERO_SIDE_ROUTE, ".before.case_count").textContent).toBe("0");

    // The pair says the absence out loud, beside the side that was measured: `ScorePair`'s one
    // documented unknown state, and never a zero standing in for a score nobody took.
    expect(
      within(scope).getByRole("group", { name: "score before unknown, score after 0.592181" }),
    ).toBeTruthy();
    expect(figureAt(scope, ZERO_SIDE_ROUTE, ".after.score").textContent).toBe("0.592181");
    expect(within(scope).getAllByTestId("score-pair-join").at(0)?.textContent).toBe("->");

    faithful(container, { [ZERO_SIDE_ROUTE]: zeroSide.text });
    mkdirSync(dirname(ZERO_SIDE_HTML), { recursive: true });
    writeFileSync(ZERO_SIDE_HTML, container.innerHTML, "utf8");
    log.push("/evals/compare: a zero-case side is an explained `unknown`, never a `0.0` (FR-47)");
  });

  it("says no case moved where a list is empty, and never leaves a blank", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(ZERO_SIDE_ROUTE)]: { status: zeroSide.code, text: zeroSide.text },
    });
    await renderCompare({ before: ZERO_ID, after: AFTER_ID });

    const body = sent(zeroSide.text);
    expect(body.regressions).toEqual([]);
    expect(body.soft_drops).toEqual([]);
    expect(body.missing_cases).toEqual([]);
    expect(NO_MOVEMENT).toBe("no case moved between these eval runs");

    for (const field of ["regressions", "soft_drops", "missing_cases"]) {
      const scope = screen.getByTestId(`list-${field}`);
      expect(within(scope).getByTestId(`empty-${field}`).textContent).toBe(NO_MOVEMENT);
      expect(within(scope).queryByRole("table")).toBeNull();
      expect((scope.textContent ?? "").trim()).not.toBe("");
    }
    // The one list the gateway did fill is still a list, beside the three sentences.
    const newCases = screen.getByTestId("list-new_cases");
    expect(within(newCases).queryByTestId("empty-new_cases")).toBeNull();
    expect(within(newCases).getAllByRole("listitem")).toHaveLength(body.new_cases.length);
    log.push("/evals/compare: an empty list reads `no case moved between these eval runs`");
  });
});

// ---- A URL missing a side ------------------------------------------------------------------------

describe("/evals/compare asks for the two ids it has not been given (FR-56, FR-60)", () => {
  it("offers a lookup per side and sends nothing while either id is missing", async () => {
    const urls: Params[] = [{}, { before: BEFORE_ID }, { after: AFTER_ID }];
    for (const params of urls) {
      const spy = answersWith({ [HEALTH_URL]: HEALTH_OK });
      const { container } = await renderCompare(params);

      expect(screen.getByTestId("compare-needs").textContent).toBe(NEEDS_BOTH);
      expect(screen.queryByRole("region", { name: COMPARE_TITLE })).toBeNull();
      // No comparison was asked for, so no figure is on the page (`D-6`).
      expect(container.querySelectorAll("[data-figure]")).toHaveLength(0);
      expect(spy.mock.calls.map((call) => call[0])).toEqual([HEALTH_URL]);

      for (const side of SIDES) {
        const group = screen.getByRole("group", { name: side });
        expect(within(group).getByLabelText("eval run id")).toBeTruthy();
      }
      cleanup();
    }
    log.push("/evals/compare: a URL missing a side offers an `IdLookup` per side and asks nothing");
  });

  it("opens the comparison at what was typed, keeping the side the URL already held", async () => {
    answersWith({ [HEALTH_URL]: HEALTH_OK });
    await renderCompare({ before: BEFORE_ID });

    const group = screen.getByRole("group", { name: "after" });
    const field = within(group).getByLabelText("eval run id") as HTMLInputElement;
    const open = within(group).getByRole("button", { name: "Open" }) as HTMLButtonElement;
    const form = group.querySelector("form") as HTMLFormElement;

    // A blank field can be submitted nowhere, so the one submission that would go nowhere cannot.
    expect(open.disabled).toBe(true);
    fireEvent.submit(form);
    expect(pushed).toEqual([]);

    fireEvent.change(field, { target: { value: ` ${AFTER_ID} ` } });
    expect(open.disabled).toBe(false);
    fireEvent.submit(form);
    // What was typed reaches the URL exactly as typed, beside the side the URL already held.
    expect(pushed).toEqual([`/evals?before=${BEFORE_ID}&after= ${AFTER_ID} `]);
    expect(sideHref("after", ` ${AFTER_ID} `, BEFORE_ID)).toBe(pushed[0]);
    expect(sideHref("before", BEFORE_ID, undefined)).toBe(`/evals?before=${BEFORE_ID}`);
    expect(sideHref("after", AFTER_ID, undefined)).toBe(`/evals?after=${AFTER_ID}`);
    log.push("/evals/compare: a typed id opens the comparison verbatim, keeping the other side");
  });

  it("shows the gateway's own 400 `ParameterError` for a request sent with a side missing", async () => {
    // The URL can carry a key with nothing in it. Whether an empty id is servable is the
    // gateway's answer to give, so the characters are sent and its own refusal is what shows.
    const route = compareRoute(BEFORE_ID, "");
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(route)]: { status: missingParameter.code, text: missingParameter.text },
    });
    await renderCompare({ before: BEFORE_ID, after: "" });

    const body = JSON.parse(missingParameter.text) as { error: string; message: string };
    expect(body.error).toBe("ParameterError");
    const shown = panel().textContent ?? "";
    expect(shown).toContain(body.error);
    expect(shown).toContain(body.message);
    expect(shown).toContain("the 'after' query parameter is required and was not given.");
    log.push("/evals/compare: a request missing a side shows the gateway's own 400 `ParameterError`");
  });
});

// ---- Reload, and the shell the page is served inside ---------------------------------------------

describe("/evals/compare is reload-safe and names each side's provider (FR-56, FR-72)", () => {
  it("renders the same comparison again from the same URL, remembering nothing", async () => {
    const first = answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(REGRESSION_ROUTE)]: { status: regression.code, text: regression.text },
    });
    const opened = await renderCompare({ before: BEFORE_ID, after: AFTER_ID });
    const before = renderedFigures(opened.container);
    expect(first.mock.calls.map((call) => call[0])).toContain(proxied(REGRESSION_ROUTE));
    cleanup();

    // A reload is a fresh read with nothing kept: the URL is the whole of the state (`D-6`).
    const again = answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(REGRESSION_ROUTE)]: { status: regression.code, text: regression.text },
    });
    const reopened = await renderCompare({ before: BEFORE_ID, after: AFTER_ID });
    expect(renderedFigures(reopened.container)).toEqual(before);
    expect(again.mock.calls.map((call) => call[0])).toContain(proxied(REGRESSION_ROUTE));
    log.push("/evals/compare: a reload of the same URL renders the same comparison");
  });

  it("serves each side's provider and model as figures, and decision 40's copy is gone", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(REGRESSION_ROUTE)]: { status: regression.code, text: regression.text },
    });
    const { container } = await renderCompare({ before: BEFORE_ID, after: AFTER_ID });

    expect(screen.queryByTestId("provider-caveat")).toBeNull();
    expect(document.body.textContent).not.toContain("does not record which provider");
    const sent = JSON.parse(regression.text) as Record<string, { provider: string | null }>;
    for (const side of ["before", "after"]) {
      for (const field of ["provider", "model"]) {
        const figure = container.querySelector(`[data-figure][data-path=".${side}.${field}"]`);
        expect(figure).not.toBeNull();
        if (sent[side]?.provider === null) {
          expect(figure?.textContent).toBe("unknown");
          expect(nullReasonFor(REGRESSION_ROUTE, `.${side}.${field}`)).toBe(
            "recorded before Harnessed recorded the provider and model",
          );
        }
      }
    }
    log.push("/evals/compare: each side's provider and model, served; decision 40's line gone");
  });

  it("puts the origin the page was served from in the line an operator copies", async () => {
    vi.stubGlobal("matchMedia", (query: string) => ({ matches: false, media: query }));
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(REGRESSION_ROUTE)]: { status: regression.code, text: regression.text },
    });
    await renderCompare({ before: BEFORE_ID, after: AFTER_ID });

    const chip = figureAt(panel(), REGRESSION_ROUTE, ".before.score");
    // Enter opens a chip; nothing else does, so it is not a trap for any other key.
    fireEvent.keyDown(chip, { key: "ArrowRight" });
    expect(screen.queryByRole("dialog")).toBeNull();
    fireEvent.keyDown(chip, { key: "Enter" });

    const dialog = screen.getByRole("dialog");
    // No endpoint string anywhere in the dialog: not the route, not the gateway's own origin.
    expect(within(dialog).getByRole("heading").textContent).toBe("Where this came from");
    expect(dialog.textContent).not.toContain(REGRESSION_ROUTE);
    expect(dialog.textContent).not.toContain(GATEWAY);
    // The operator's copyable line is the CLI `--json` twin, keyed on the route's own path
    // (decision 43's relative origin is what `fetchPanel` reads at, proved in `lib/fetch-panel.ts`).
    expect(within(dialog).getByTestId("cli-twin").textContent).toBe(
      "harnessed eval compare <before> <after> --json",
    );
    expect(within(dialog).getByTestId("record-view")).toBeTruthy();
    expect(within(dialog).getByTestId("cited").textContent).toBeTruthy();

    const controls = within(dialog).getAllByRole("button");
    const first = controls.at(0) as HTMLElement;
    const last = controls.at(-1) as HTMLElement;
    last.focus();
    fireEvent.keyDown(dialog, { key: "Tab" });
    expect(document.activeElement).toBe(first);
    fireEvent.keyDown(dialog, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(last);
    fireEvent.keyDown(dialog, { key: "ArrowDown" });

    const written: string[] = [];
    vi.stubGlobal("navigator", {
      clipboard: {
        writeText: async (line: string) => {
          written.push(line);
        },
      },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Copy" }));
    expect(written).toEqual(["harnessed eval compare <before> <after> --json"]);

    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(chip);
    log.push("/evals/compare: a figure opens the comparison it was read out of, with its CLI twin");
  });

  it("is reachable from the narrow menu as well as from the sidebar", async () => {
    answersWith({ [HEALTH_URL]: HEALTH_OK });
    await renderCompare({});

    const menu = screen.getByRole("button", { name: "Menu" });
    expect(screen.getByRole("navigation").getAttribute("data-open")).toBe("false");
    fireEvent.click(menu);
    expect(screen.getByRole("navigation").getAttribute("data-open")).toBe("true");
    expect(
      within(screen.getByRole("navigation")).getByRole("link", { name: "Evals" }),
    ).toBeTruthy();
    log.push("/evals/compare: the page is reachable from the narrow menu and from the sidebar");
  });
});

// ---- The states design.md lets no panel skip ------------------------------------------------------

describe("the comparison panel wears every state, and remembers nothing (design.md)", () => {
  it("is busy before an answer and holds the answer after it", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(REGRESSION_ROUTE)]: { status: regression.code, text: regression.text },
    });
    const page = pageAt(CompareView, { before: BEFORE_ID, after: AFTER_ID });
    render(shell(page));

    expect(panel().getAttribute("aria-busy")).toBe("true");
    expect(screen.getAllByTestId("skeleton-row").length).toBeGreaterThan(0);
    // The pill has not heard from its own poll yet either, and says so rather than guessing.
    expect(screen.getByTestId("gateway-status-pill").textContent).toContain("checking");

    await settle();
    expect(panel().getAttribute("aria-busy")).toBe("false");
    expect(screen.queryAllByTestId("skeleton-row")).toEqual([]);
    log.push("panel: `loading` is the busy skeleton, and the comparison replaces it");
  });

  it("names the route, the status and the byte count, on the dashboard's own clock", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(REGRESSION_ROUTE)]: { status: regression.code, text: regression.text },
    });
    await renderCompare({ before: BEFORE_ID, after: AFTER_ID });

    const footer = panel().querySelector("footer");
    expect(footer?.textContent).toContain("Updated 0 min ago");
    expect(footer?.textContent).not.toContain(REGRESSION_ROUTE);
    expect(footer?.textContent).not.toContain("200");
    expect(footer?.textContent).not.toContain("fetched");
    log.push("panel: the footer states only freshness and Refresh, no endpoint string (D-9)");
  });

  // Slow by nature: renders the page twice across a five-minute fake-clock jump; 4.0 s on the operator's PC.
  it("goes stale five minutes after it fetched, and a refetch clears the badge", { timeout: 15_000 }, async () => {
    vi.useFakeTimers();
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(REGRESSION_ROUTE)]: { status: regression.code, text: regression.text },
    });
    const page = pageAt(CompareView, { before: BEFORE_ID, after: AFTER_ID });
    render(shell(page));

    // Settle the read first: the elapsed time this test means is time after the answer arrived.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(screen.getByTestId("stale-badge").textContent).toBe("Updated 0 min ago");
    expect(screen.getByTestId("stale-badge").getAttribute("data-warn")).toBe("false");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5 * 60 * 1000);
    });
    expect(screen.getByTestId("stale-badge").textContent).toBe("Updated 5 min ago");
    expect(screen.getByTestId("stale-badge").getAttribute("data-warn")).toBe("true");
    expect(screen.getByTestId("panel-announcement").textContent).toBe("Updated 5 min ago");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60 * 1000);
    });
    expect(screen.getByTestId("stale-badge").textContent).toBe("Updated 6 min ago");
    expect(screen.getByTestId("panel-announcement").textContent).toBe("Updated 5 min ago");

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(screen.getByTestId("stale-badge").textContent).toBe("Updated 0 min ago");
    expect(screen.getByTestId("stale-badge").getAttribute("data-warn")).toBe("false");
    expect(screen.getByTestId("panel-announcement").textContent).toBe("");
    log.push("panel: `stale` at five minutes, announced once, cleared by `Refresh`");
  });

  it("renders no figure from memory when the gateway cannot be reached", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(REGRESSION_ROUTE)]: "rejects",
    });
    const { container } = await renderCompare({ before: BEFORE_ID, after: AFTER_ID });

    expect(screen.getByRole("heading", { name: "Gateway unreachable" })).toBeTruthy();
    expect(container.querySelectorAll("[data-figure]")).toHaveLength(0);
    // A failed data read never rewrites the pill, which reads its own poll (decision 34).
    expect(screen.getByTestId("gateway-status-pill").getAttribute("data-state")).toBe("reachable");
    cleanup();

    answersWith({ [HEALTH_URL]: HEALTH_OK, [proxied(REGRESSION_ROUTE)]: "rejects" });
    await renderCompare({ before: BEFORE_ID, after: AFTER_ID });
    expect(screen.getByTestId("unreachable-cause").textContent).toBe("ECONNREFUSED");
    cleanup();

    answersWith({ [HEALTH_URL]: HEALTH_OK, [proxied(REGRESSION_ROUTE)]: "rejects silently" });
    await renderCompare({ before: BEFORE_ID, after: AFTER_ID });
    expect(screen.getByTestId("unreachable-cause").textContent).toBe("TypeError");
    cleanup();

    // A refusal that is not an `Error` has no name or message to read, so it is quoted as itself.
    answersWith({ [HEALTH_URL]: HEALTH_OK, [proxied(REGRESSION_ROUTE)]: "rejects with no error" });
    await renderCompare({ before: BEFORE_ID, after: AFTER_ID });
    expect(screen.getByTestId("unreachable-cause").textContent).toBe(
      "the browser refused the request",
    );
    cleanup();

    log.push("panel: an unreachable gateway leaves 0 figures, and names only the cause it gave");
  });

  it("quotes a body that is not the error contract as itself, and offers the read again", async () => {
    const spy = answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(REGRESSION_ROUTE)]: { status: 500, text: "the gateway fell over" },
    });
    await renderCompare({ before: BEFORE_ID, after: AFTER_ID });

    const shown = panel().textContent ?? "";
    expect(shown).toContain("UnreadableBody");
    expect(shown).toContain("the gateway fell over");

    const before = spy.mock.calls.length;
    await act(async () => {
      fireEvent.click(
        within(panel()).getAllByRole("button", { name: "Refetch" }).at(0) as HTMLElement,
      );
    });
    expect(spy.mock.calls.length).toBeGreaterThan(before);
    log.push("panel: a body outside decision 24's contract is quoted as itself, and read again");
  });

  it("lets the pill speak for its own poll while the comparison renders", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(REGRESSION_ROUTE)]: { status: regression.code, text: regression.text },
    });
    await renderCompare({ before: BEFORE_ID, after: AFTER_ID });

    const pill = screen.getByTestId("gateway-status-pill");
    expect(pill.getAttribute("data-state")).toBe("reachable");
    expect(screen.getByTestId("pill-announcement").textContent).toBe("gateway reachable");

    fireEvent.click(pill);
    const dialog = screen.getByRole("dialog");
    // The pill cites no path, so its drawer highlights nothing rather than guessing at one.
    expect(within(dialog).getByTestId("record-view")).toBeTruthy();
    expect(within(dialog).queryByTestId("cited")).toBeNull();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(screen.getByTestId("gateway-status-pill"));
    cleanup();

    answersWith({
      [HEALTH_URL]: "rejects",
      [proxied(REGRESSION_ROUTE)]: { status: regression.code, text: regression.text },
    });
    await renderCompare({ before: BEFORE_ID, after: AFTER_ID });
    const down = screen.getByTestId("gateway-status-pill");
    expect(down.getAttribute("data-state")).toBe("unreachable");
    expect((down as HTMLButtonElement).disabled).toBe(true);
    cleanup();

    answersWith({
      [HEALTH_URL]: { status: 500, text: '{"error":"InternalError","message":"it failed"}' },
      [proxied(REGRESSION_ROUTE)]: { status: regression.code, text: regression.text },
    });
    await renderCompare({ before: BEFORE_ID, after: AFTER_ID });
    expect(screen.getByTestId("gateway-status-pill").getAttribute("data-state")).toBe("error");
    expect(screen.getByTestId("gateway-status-pill").textContent).toContain("gateway error 500");
    log.push("panel: the pill's four words are its own poll's, never the comparison's");
  });
});

// ---- Where each figure says it came from, proved against the bytes ------------------------------

describe("every figure opens the characters it was read out of (FR-51, `D-4`)", () => {
  // Slow by nature: opens a drawer for every figure on the page; 3.6 s on the operator's PC.
  it("cites its own path in the raw body, for every figure the comparison put on the page", { timeout: 15_000 }, async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(REGRESSION_ROUTE)]: { status: regression.code, text: regression.text },
    });
    await renderCompare({ before: BEFORE_ID, after: AFTER_ID });

    const scope = panel();
    const chips = [...scope.querySelectorAll<HTMLElement>("[data-figure]")].filter(
      (chip) => chip.getAttribute("data-route") === REGRESSION_ROUTE && chip.tagName === "BUTTON",
    );
    // Every figure but the two row links, which are anchors carrying their own provenance and
    // open nowhere - they are `<a>` figures, not `Value` buttons.
    expect(chips).toHaveLength(22);

    for (const chip of chips) {
      // The gateway's own route, the same characters `data-route` carries - never a proxy path.
      expect(chip.getAttribute("data-route")).toBe(REGRESSION_ROUTE);
      fireEvent.click(chip);
      const dialog = screen.getByRole("dialog");
      // The record view is the response's own bytes, and the cited span is inside it.
      expect(within(dialog).getByTestId("record-view")).toBeTruthy();
      expect(within(dialog).getByTestId("cited").textContent).toBeTruthy();
      fireEvent.keyDown(document, { key: "Escape" });
    }
    log.push(`/evals/compare: all ${chips.length} chips cite a span of the comparison's own bytes`);
  });

  it("marks the occurrence the path names, not the first one that looks like it", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(REGRESSION_ROUTE)]: { status: regression.code, text: regression.text },
    });
    await renderCompare({ before: BEFORE_ID, after: AFTER_ID });

    // Three paths whose characters recur in this body: `2` is in five fields, `0.0` is the second
    // half of one `ScoreDelta`, and a case id sits in an array with no key in front of it.
    const pins = [
      { path: ".after.pass_count", cited: "2" },
      { path: ".regressions[0].score_after", cited: "0.0" },
      { path: ".new_cases[0]", cited: "case-d" },
    ];

    for (const pin of pins) {
      const chip = figureAt(panel(), REGRESSION_ROUTE, pin.path);
      fireEvent.click(chip);

      const dialog = screen.getByRole("dialog");
      const mark = within(dialog).getByTestId("cited");
      // The occurrence highlighted is the one the path names, structurally - never a look-alike
      // elsewhere in the body, and never the first match a string search would find.
      expect(mark.textContent).toBe(pin.cited);
      expect(mark.getAttribute("aria-current")).toBe("true");
      fireEvent.keyDown(document, { key: "Escape" });
    }
    log.push("/evals/compare: a chip marks the field its path names, never a look-alike earlier on");
  });

  it("opens without motion where the operator asked for none (design.md)", async () => {
    vi.stubGlobal("matchMedia", (query: string) => ({ matches: true, media: query }));
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(REGRESSION_ROUTE)]: { status: regression.code, text: regression.text },
    });
    await renderCompare({ before: BEFORE_ID, after: AFTER_ID });

    const chip = figureAt(panel(), REGRESSION_ROUTE, ".before.score");
    fireEvent.click(chip);
    expect(screen.getByRole("dialog").getAttribute("data-motion")).toBe("instant");
    log.push("/evals/compare: a reduced-motion setting is honoured by the provenance drawer");
  });

  it("gives a figure no source to open where no panel fetched one (MUST rule 21)", () => {
    // The chip is the panel's response or it is nothing: a figure rendered outside a panel still
    // names its route and its path, and offers no control that would open a response it never had.
    const wire = parseWire(regression.text, REGRESSION_ROUTE);
    const loose = render(<Value leaf={wire.at(".before.score")} />);
    expect(figureAt(loose.container, REGRESSION_ROUTE, ".before.score").textContent).toBe("0.829218");
    expect(figuresWithoutSource(loose.container)).toEqual([]);
    expect(loose.container.querySelector("button")).toBeNull();
    cleanup();

    // And an absent figure is the word and its documented reason, panel or no panel (`D-2`).
    const zero = parseWire(zeroSide.text, ZERO_SIDE_ROUTE);
    const absent = render(
      <Value
        leaf={zero.at(".before.score")}
        nullReason={nullReasonFor(ZERO_SIDE_ROUTE, ".before.score")}
      />,
    );
    expect(absent.container.textContent).toContain("unknown");
    expect(absent.container.textContent).toContain("no evidence: 0 cases");
    expect(absent.container.querySelector("button")).toBeNull();
    cleanup();

    // A null the design documents no reason for still reads `unknown`, and says only that: the
    // word is what an absence renders as, with or without a sentence to go beside it (`D-2`).
    const bare = render(<Value leaf={zero.at(".before.score")} />);
    expect(bare.container.textContent).toBe("unknown");
    expect(bare.container.querySelector("[data-figure]")?.getAttribute("aria-label")).toBe(
      "unknown",
    );
    log.push("/evals/compare: a figure with no panel behind it offers no source to open");
  });

  it("names the relative origin for the route this panel read (decision 43)", () => {
    // The drawer's `curl` line is built from the origin, which already carries `/v1`, so a route
    // that spells the version is not made to spell it twice and one that does not is appended.
    expect(gatewayHref(GATEWAY, REGRESSION_ROUTE)).toBe(
      `${GATEWAY}/eval/compare?before=${BEFORE_ID}&after=${AFTER_ID}`,
    );
    expect(gatewayHref(GATEWAY, COMPARE_ROUTE)).toBe(`${GATEWAY}/eval/compare`);
    expect(gatewayHref(GATEWAY, "/eval/compare")).toBe(`${GATEWAY}/eval/compare`);
    log.push("/evals/compare: the `curl` line names the gateway once, at the relative `/v1` origin");
  });

  it("renders nothing from a read that came back after the page was left (`D-6`)", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(REGRESSION_ROUTE)]: { status: regression.code, text: regression.text },
    });
    const page = pageAt(CompareView, { before: BEFORE_ID, after: AFTER_ID });
    render(shell(page));

    // Leaving before either read answers: what comes back belongs to a page that is gone, and
    // nothing here holds it for the next one.
    cleanup();
    await settle();
    expect(screen.queryByRole("region", { name: COMPARE_TITLE })).toBeNull();
    expect(document.body.textContent).toBe("");
    log.push("panel: an answer that arrives after the page was left is rendered nowhere (`D-6`)");
  });

  it("keys its CLI twin and its documented nulls on the compare route alone", () => {
    expect(cliTwinFor(REGRESSION_ROUTE)).toBe("harnessed eval compare <before> <after> --json");
    // The twin is keyed on the route's path, so the two ids never change which command it is.
    expect(cliTwinFor(COMPARE_ROUTE)).toBe(cliTwinFor(REGRESSION_ROUTE));
    expect(cliTwinFor(`${COMPARE_ROUTE}/extra`)).toBeUndefined();

    // Only the two side scores have a documented absence here. A pass count of 0 is a count, so
    // asking for a reason beside one would print "no evidence" next to the evidence (ticket 107).
    expect(nullReasonFor(REGRESSION_ROUTE, ".before.score")).toBe("no evidence: 0 cases");
    expect(nullReasonFor(REGRESSION_ROUTE, ".after.score")).toBe("no evidence: 0 cases");
    expect(nullReasonFor(REGRESSION_ROUTE, ".before.pass_count")).toBeUndefined();
    expect(nullReasonFor(`${COMPARE_ROUTE}/extra`, ".before.score")).toBeUndefined();

    // A 404 and zero rows on this route mean what the gateway's own words say, and nothing more.
    expect(absentTextFor(REGRESSION_ROUTE)).toBeUndefined();
    expect(emptyTextFor(REGRESSION_ROUTE)).toBeUndefined();
    log.push("/evals/compare: the route's twin and its two documented nulls, and no third sentence");
  });
});

// ---- Evidence --------------------------------------------------------------------------------------

describe("evidence", () => {
  it("writes the manifest this ticket declares", () => {
    expect(log.length).toBeGreaterThan(10);
    mkdirSync(dirname(EVIDENCE), { recursive: true });
    writeFileSync(EVIDENCE, `${log.join("\n")}\n`, "utf8");
  });
});

});

describe("improvement (ticket 109)", () => {
const DASHBOARD = resolve(import.meta.dirname, "../..");
const REPO = resolve(DASHBOARD, "..");
const GATEWAY_FIXTURES = resolve(DASHBOARD, "tests/fixtures/gateway");
const EVIDENCE = resolve(REPO, "evidence/109/improve.txt");
const PROMOTIONS_HTML = resolve(REPO, "evidence/109/promotions.html");
const UNDECIDED_HTML = resolve(REPO, "evidence/109/undecided-cycle.html");
const DECIDED_HTML = resolve(REPO, "evidence/109/decided-cycle.html");

/** The gateway this test's shell is pointed at. Not the operator's 8756 (decision 30). */
const GATEWAY = GATEWAY_ORIGIN;

/** The lines this run writes to `evidence/109/improve.txt`. */
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

const promotions = recorded("promotions");
const noPromotions = recorded("empty.promotions");
const promotedCycle = recorded("improve.cycle.promoted");
const undecidedCycle = recorded("improve.cycle.undecided");
const baselineOnly = recorded("improve.cycle.baseline-only");
const dryRun = recorded("improve.cycle.dry-run");
const unknownCycle = recorded("notfound.improve.cycle");
const health = recorded("health");

/** The improvement run ids the recordings hold, taken from the routes they answered. */
const PROMOTED_ID = "imp_000000000E6Q9ACKRYS2BJ6ZB7";
const UNDECIDED_ID = "imp_000000000J7TMSF08D899W4ZWR";
const BASELINE_ID = "imp_000000000K6M9M70BTX1CYT17Z";
const DRY_RUN_ID = "imp_0000000009C4C5522R2BFNNJC5";
const UNKNOWN_ID = "imp_00000000000000000000000000";

/** The six fields FR-46 sends as `null` where the cycle reached no decision. */
const UNDECIDED_FIELDS = [
  ".score_before",
  ".score_after",
  ".pass_count_before",
  ".pass_count_after",
  ".regressions",
  ".soft_drops",
];

/**
 * A second promotion row, written here because no recording holds two and the claim under test is
 * that the rows keep the gateway's own decided order. It is not a recorded fixture and nothing
 * here edits one (MUST rule 24); it is the same shape `memory/procedural.py::promotions` serves,
 * newest decision first, with the recorded row second.
 */
const TWO_DECISIONS = [
  '{"promotions":[',
  '{"cand_id":"cand_declined","decided_at":"2026-01-02T12:00:00Z","passes_after":4,',
  '"passes_before":5,"promoted":false,"reason":"regression","regressions":1,',
  '"score_after":0.6,"score_before":0.8,"soft_drops":0},',
  '{"cand_id":"cand_4820460a275e0bc8","decided_at":"2026-01-01T12:00:00Z","passes_after":5,',
  '"passes_before":4,"promoted":true,"reason":"score","regressions":0,',
  '"score_after":1.0,"score_before":0.8,"soft_drops":0}',
  "]}",
].join("");

// ---- One answer per route, so nothing here is a network ----------------------------------------

type Answer = { status: number; text: string; headers?: Record<string, string> };

/** A response object with only what `fetch-panel` reads. */
function answer({ status, text, headers = {} }: Answer): Response {
  return {
    status,
    ok: status === 200,
    headers: { get: (name: string) => headers[name.toLowerCase()] ?? null },
    text: async () => text,
  } as unknown as Response;
}


type Route = Answer | "rejects" | "rejects silently" | "rejects with no error";

/** Stub `fetch` with one answer per gateway route, and hand back the spy so a test can read the URLs. */
function answersWith(routes: Record<string, Route>): ReturnType<typeof vi.fn> {
  const spy = vi.fn(async (url: string) => {
    const next = routes[url];
    if (next === undefined) throw new Error(`no answer stubbed for ${url}`);
    if (next === "rejects") throw new Error("ECONNREFUSED");
    // A browser that refuses a fetch without a message: the class name is all there is to report.
    if (next === "rejects silently") throw new TypeError("");
    // And a refusal that is not an `Error` at all: reported as itself, never as an invented code.
    if (next === "rejects with no error") throw "the browser refused the request";
    return answer(next);
  });
  vi.stubGlobal("fetch", spy);
  return spy;
}

/** The recorded `/health` the shell's pill polls, so no test here is about the pill. */
const HEALTH_URL = "/v1/health";
const HEALTH_OK: Answer = { status: health.code, text: health.text };

/** The proxy URL a gateway route is read through. */
function proxied(route: string): string {
  return route;
}

/** The route `/improve` reads at the limit the URL carries. */
const LISTING_ROUTE = promotionsRoute(DEFAULT_LIMIT);

/** Let React settle a read. Every state change after this is a render, never a request. */
async function settle(): Promise<void> {
  await act(async () => {});
}

/** The shell as `app/layout.tsx` mounts it, around whatever a page renders. */
function shell(children: ReactNode) {
  return <AppShell>{children}</AppShell>;
}

/** `/improve` at whatever the URL carries, rendered as the server renders it. */
async function renderImprove(searchParams: { limit?: string } = {}) {
  const page = pageAt(ImproveView, searchParams);
  const rendered = render(shell(page));
  await settle();
  return rendered;
}

/** `/improve?id=`, at the id the URL carried (FR-64). */
async function renderCycle(improvementRunId: string) {
  const page = pageAt(ImproveView, { id: improvementRunId });
  const rendered = render(shell(page));
  await settle();
  return rendered;
}

/** One panel, so the shell's own regions never answer for it. */
function panel(title: string): HTMLElement {
  return screen.getByRole("region", { name: title });
}

/**
 * The figure at a route and path, found by its provenance pair rather than by its `title`:
 * `SourceChip` carries the same `route -> path` title, and only the figure carries `data-path`.
 */
function figureAt(scope: HTMLElement, route: string, path: string): HTMLElement {
  const found = scope.querySelector<HTMLElement>(`[data-route="${route}"][data-path="${path}"]`);
  if (found === null) throw new Error(`no figure for ${route} -> ${path}`);
  return found;
}

/**
 * What a `PassMark` says for itself: its field name and its figure, without the `SourceChip` the
 * figure carries beside it. Reading the whole element would fold the chip's own label into the
 * word under test, which is ticket 106's trap in a new place.
 */
function markText(mark: HTMLElement): string {
  const label = mark.firstElementChild?.textContent ?? "";
  const figure = mark.querySelector("[data-figure]")?.textContent ?? "";
  return `${label}${figure}`;
}

/**
 * Every control on a page that is not one of the read-only four (`D-8`): refetch, open a source,
 * copy, navigate. The shell's own status pill opens its poll's response and is one of them.
 */
function writingControls(container: ParentNode, allowed: Set<string>): string[] {
  return [...container.querySelectorAll("button")]
    .filter((button) => button.getAttribute("data-testid") !== "gateway-status-pill")
    // A figure is its own source-opening trigger now (`Value` is a `button`): it is read-only by
    // construction, so it is excluded by the hook that marks it rather than by its own text.
    .filter((button) => !button.hasAttribute("data-figure"))
    // The theme toggle keeps a choice in this browser (ticket 251); it writes nothing to the gateway.
    .filter((button) => !/^Switch to (light|dark) theme$/.test(button.getAttribute("aria-label") ?? ""))
    .map((button) => button.getAttribute("aria-label") ?? button.textContent ?? "")
    .filter((name) => !allowed.has(name));
}

/** Every figure of a tree, checked against the bytes of the bodies it claims to come from. */
function faithful(container: ParentNode, bodies: Record<string, string>): void {
  const figures = renderedFigures(container);
  expect(figures.length).toBeGreaterThan(0);
  expect(figuresWithoutSource(container)).toEqual([]);
  // A promotion's "decided" reads in words (ticket 303); its exact stamp is the tooltip's.
  const words = figures.filter((f) => !/^\.promotions\[\d+\]\.decided_at$/.test(f.path));
  expect(divergences(words, new Map(Object.entries(bodies)))).toEqual([]);
}

/** Write one page's rendered DOM where the ticket's evidence manifest says it goes. */
function capture(path: string, container: ParentNode): void {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, (container as HTMLElement).innerHTML, "utf8");
}

/** The one promotion the recording holds, as the gateway sent it. */
type SentPromotion = {
  cand_id: string;
  decided_at: string;
  promoted: boolean;
  reason: string;
  score_before: number;
  score_after: number;
  passes_before: number;
  passes_after: number;
  regressions: number;
  soft_drops: number;
};

function sentPromotions(text: string): SentPromotion[] {
  return (JSON.parse(text) as { promotions: SentPromotion[] }).promotions;
}

/** One cycle, as the gateway sent it. */
type SentCycle = {
  improvement_run_id: string;
  suite: string;
  cand_id: string;
  candidate: boolean;
  promoted: boolean;
  rule: string;
  steps: string[];
  scored: number;
  score_before: number | null;
  score_after: number | null;
};

function sentCycle(text: string): SentCycle {
  return JSON.parse(text) as SentCycle;
}

beforeEach(() => {
  pushed.length = 0;
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

afterAll(() => {
  mkdirSync(dirname(EVIDENCE), { recursive: true });
  writeFileSync(EVIDENCE, `${log.join("\n")}\n`, "utf8");
});

// ---- One request, to the promotions route --------------------------------------------------------

describe("/improve reads one route, the promotions route (FR-57)", () => {
  it("asks the route the recording answered, carrying the limit the URL held", async () => {
    const spy = answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(LISTING_ROUTE)]: { status: promotions.code, text: promotions.text },
    });
    await renderImprove();

    // The route this page builds is the route the gateway recorded itself answering, plus the
    // ceiling the operator asked for.
    expect(LISTING_ROUTE).toBe(`${promotions.route}?limit=${DEFAULT_LIMIT}`);
    expect(DEFAULT_LIMIT).toBe("20");
    expect([...LIMITS]).toEqual(["20", "50", "100"]);

    const asked = spy.mock.calls.map((call) => call[0] as string);
    expect(asked.filter((url) => url !== HEALTH_URL)).toEqual([proxied(LISTING_ROUTE)]);
    // The cycle route belongs to the other page: nothing here reads a cycle it was not asked for.
    expect(asked.some((url) => url.includes("/improve/cycles"))).toBe(false);
    log.push("/improve: one request, to /v1/promotions?limit=20, and none to a cycle");
  });

  it("passes a limit the gateway may refuse straight through, unedited", async () => {
    const route = promotionsRoute("nought");
    const spy = answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(route)]: { status: promotions.code, text: promotions.text },
    });
    await renderImprove({ limit: "nought" });

    // Whether a limit is servable is the gateway's answer to give, so its characters are sent.
    expect(spy.mock.calls.map((call) => call[0])).toContain(proxied(route));
    expect(listingHref("50")).toBe("/evals?limit=50");
    log.push("/improve: a limit the URL holds reaches the gateway unedited, whatever it says");
  });

  it("keeps the chosen limit in the URL and nowhere else", async () => {
    // Ticket 207: the one limit control lives in the eval runs listing and drives both.
    const evalRuns = recorded("eval.runs.listing");
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(`/v1/eval/runs?limit=${DEFAULT_LIMIT}`)]: { status: evalRuns.code, text: evalRuns.text },
      [proxied(LISTING_ROUTE)]: { status: promotions.code, text: promotions.text },
    });
    render(shell(pageAt(EvalsPage, {})));
    await settle();

    const control = screen.getByTestId("limit-control");
    const fifty = within(control).getByLabelText("50") as HTMLInputElement;
    expect((within(control).getByLabelText("20") as HTMLInputElement).checked).toBe(true);
    fireEvent.click(fifty);
    expect(pushed).toEqual([listingHref("50")]);
    log.push("/evals: the rows-to-ask-for choice is a navigation, never a remembered byte");
  });
});

// ---- The promotion history ------------------------------------------------------------------------

describe("/improve renders the promotion history as sent (FR-57, UJ-8)", () => {
  it("shows promoted, reason, both scores, both pass counts, regressions and soft drops", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(LISTING_ROUTE)]: { status: promotions.code, text: promotions.text },
    });
    const { container } = await renderImprove();

    const rows = sentPromotions(promotions.text);
    expect(rows).toHaveLength(1);
    const chars = sourceCharactersByPath(promotions.text);
    const scope = panel(PROMOTIONS_TITLE);

    for (const field of [
      "cand_id",
      "promoted",
      "reason",
      "score_before",
      "score_after",
      "passes_before",
      "passes_after",
      "regressions",
      "soft_drops",
    ]) {
      const path = `.promotions[0].${field}`;
      expect(figureAt(scope, LISTING_ROUTE, path).textContent).toBe(chars.get(path));
    }

    // `decided` reads in words (ticket 303); the exact stamp is the tooltip's.
    const decided = figureAt(scope, LISTING_ROUTE, ".promotions[0].decided_at");
    expect(decided.textContent).toMatch(/ ago$/);
    expect(decided.textContent).not.toMatch(/\d{4}-\d{2}-\d{2}T/);

    // The characters, never a re-serialized parse: `1.0` is not `1` (decision 31).
    expect(figureAt(scope, LISTING_ROUTE, ".promotions[0].score_after").textContent).toBe("1.0");
    expect(figureAt(scope, LISTING_ROUTE, ".promotions[0].score_before").textContent).toBe("0.8");
    expect(figureAt(scope, LISTING_ROUTE, ".promotions[0].reason").textContent).toBe("score");

    // `promoted` is a `PassMark` wearing its own field name: the promoter declining is not a
    // "fail", so only `passed` is ever drawn as one.
    const mark = within(scope).getByTestId("pass-mark");
    expect(markText(mark)).toBe("promoted: true");
    expect(within(scope).queryByTestId("pass-mark-word")).toBeNull();

    // The two scores are one `ScorePair`, joined by a plain `->` that carries no direction.
    expect(
      within(scope).getByRole("group", { name: "score before 0.8, score after 1.0" }),
    ).toBeTruthy();
    expect(within(scope).getByTestId("score-pair-join").textContent).toBe("->");

    expect(screen.getByTestId("promotions-footer").textContent).toBe(promotionsFooterLine(DEFAULT_LIMIT));
    expect(promotionsFooterLine(DEFAULT_LIMIT)).toContain("in decided order");

    faithful(container, { [LISTING_ROUTE]: promotions.text });
    capture(PROMOTIONS_HTML, container);
    log.push("/improve: promoted, reason, both scores, both pass counts, regressions, soft drops");
  });

  it("keeps the rows in the order the gateway decided them, and links none of them", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(LISTING_ROUTE)]: { status: promotions.code, text: TWO_DECISIONS },
    });
    const { container } = await renderImprove();

    const sent = sentPromotions(TWO_DECISIONS);
    expect(sent).toHaveLength(2);
    const scope = panel(PROMOTIONS_TITLE);
    const rows = within(within(scope).getByRole("table")).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(2);

    for (const [index, row] of rows.entries()) {
      const shown = figureAt(row as HTMLElement, LISTING_ROUTE, `.promotions[${index}].cand_id`);
      expect(shown.textContent).toBe(sent[index]?.cand_id);
    }
    // The newest decision is first because the gateway sent it first, and nothing here reorders.
    expect(sent[0]?.cand_id).toBe("cand_declined");

    // A declined promotion is its own field and its own word, never "fail".
    const marks = within(scope).getAllByTestId("pass-mark");
    expect(marks.map(markText)).toEqual(["promoted: false", "promoted: true"]);

    // A promotion row carries a `cand_id` and no cycle, suite or procedure id (`[GAP G-12]`), so
    // there is no id here to build a link out of and this page builds none (MUST rule 25).
    expect(within(scope).queryAllByRole("link")).toEqual([]);
    faithful(container, { [LISTING_ROUTE]: TWO_DECISIONS });
    log.push("/improve: two decisions render in the gateway's order, and neither row links anywhere");
  });

  it("says the ledger holds no decision yet, and what would put one there", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(LISTING_ROUTE)]: { status: noPromotions.code, text: noPromotions.text },
    });
    const { container } = await renderImprove();

    expect(sentPromotions(noPromotions.text)).toEqual([]);
    const shown = panel(PROMOTIONS_TITLE).textContent ?? "";
    expect(shown).toContain("No promotions recorded yet.");
    expect(shown).not.toContain(LISTING_ROUTE);
    expect(shown).toContain(`Create some with ${RUN_COMMAND}.`);
    expect(RUN_COMMAND).toBe("harnessed improve run");
    // Zero rows is not an error and not an absence: nothing went wrong and no figure was rendered.
    expect(container.querySelectorAll("[data-figure]")).toHaveLength(0);
    expect(screen.queryByRole("alert")).toBeNull();
    log.push("/improve: an empty ledger is `EmptyState` naming the noun, never the route");
  });
});

// ---- The one field an operator types into ---------------------------------------------------------

describe("/improve offers an improvement run id lookup (FR-57, FR-60)", () => {
  it("is labelled for the id it wants and opens the cycle at what was typed", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(LISTING_ROUTE)]: { status: promotions.code, text: promotions.text },
    });
    await renderImprove();

    const group = screen.getByRole("region", { name: "Open an improvement cycle" });
    const field = within(group).getByLabelText("improvement run id") as HTMLInputElement;
    const open = within(group).getByRole("button", { name: "Open" }) as HTMLButtonElement;
    const form = group.querySelector("form") as HTMLFormElement;

    // A blank field can be submitted nowhere, so the one submission that would go nowhere cannot.
    expect(open.disabled).toBe(true);
    fireEvent.submit(form);
    expect(pushed).toEqual([]);

    // The shape is all that is validated: whether the ledger holds it is the cycle page's 404.
    fireEvent.change(field, { target: { value: ` ${PROMOTED_ID} ` } });
    expect(open.disabled).toBe(false);
    fireEvent.submit(form);
    expect(pushed).toEqual([cycleHref(` ${PROMOTED_ID} `)]);
    const opened = new URL(pushed[0] ?? "", "http://dashboard.invalid");
    expect(opened.pathname).toBe("/evals");
    expect(opened.searchParams.get("cycle")).toBe(` ${PROMOTED_ID} `);
    log.push("/improve: the lookup opens `/evals?cycle=<id as typed>` - no trim, no split, no re-casing");
  });

  it("names the command that prints the id, because no listing offers one", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(LISTING_ROUTE)]: { status: promotions.code, text: promotions.text },
    });
    await renderImprove();

    const group = screen.getByRole("region", { name: "Open an improvement cycle" });
    const command = within(group).getByTestId("cli-command");
    expect(command.textContent).toContain(LOOKUP_NOTE);
    expect(within(command).getByTestId("cli-command-text").textContent).toBe(RUN_JSON_COMMAND);
    expect(RUN_JSON_COMMAND).toBe("harnessed improve run --json");
    log.push("/improve: the cycle id comes from `harnessed improve run --json`, and the page says so");
  });
});

// ---- One cycle ------------------------------------------------------------------------------------

describe("/improve?id= renders one decided cycle (FR-57, UJ-8)", () => {
  it("reads the cycle route with the id exactly as the URL carried it", async () => {
    const odd = ` ${PROMOTED_ID} `;
    const route = cycleRoute(odd);
    expect(route).toBe(`/v1/improve/cycles/${odd}`);
    const spy = answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(route)]: { status: unknownCycle.code, text: unknownCycle.text },
    });
    await renderCycle(odd);

    expect(spy.mock.calls.map((call) => call[0])).toContain(proxied(route));
    expect(cycleRoute(PROMOTED_ID)).toBe(promotedCycle.route);
    log.push("/improve/<id>: the id reaches the gateway as typed, and the route is the recorded one");
  });

  it("shows the steps recorded, a ScorePair for the scores and a RuleBadge with its gloss", async () => {
    const route = cycleRoute(PROMOTED_ID);
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(route)]: { status: promotedCycle.code, text: promotedCycle.text },
    });
    const { container } = await renderCycle(PROMOTED_ID);

    const body = sentCycle(promotedCycle.text);
    const chars = sourceCharactersByPath(promotedCycle.text);
    const scope = panel(CYCLE_TITLE);

    // The steps, in the response's own order, one figure each.
    const steps = within(screen.getByTestId("steps")).getAllByRole("listitem");
    expect(steps).toHaveLength(body.steps.length);
    expect(body.steps).toEqual(["baseline", "reflect", "candidate", "holdout", "promote"]);
    for (const [index, step] of body.steps.entries()) {
      expect(figureAt(scope, route, `.steps[${index}]`).textContent).toBe(step);
    }

    // The two scores as a pair, each side its own read figure and no third number between them.
    expect(figureAt(scope, route, ".score_before").textContent).toBe(chars.get(".score_before"));
    expect(figureAt(scope, route, ".score_after").textContent).toBe("1.0");
    expect(
      within(scope).getByRole("group", { name: "score before 0.8, score after 1.0" }),
    ).toBeTruthy();

    // The rule is the promoter's own word, with the static gloss beneath it (`[GAP G-13]`).
    expect(body.rule).toBe("score");
    expect(figureAt(scope, route, ".rule").textContent).toBe("score");
    expect(within(scope).getByTestId("rule-gloss").textContent).toBe(RULE_GLOSSES.score);
    expect(scope.textContent).not.toContain(UNDECIDED);

    // The counts the decision rests on, each as sent, each with no invented sentence beside it.
    for (const field of ["pass_count_before", "pass_count_after", "regressions", "soft_drops", "scored"]) {
      expect(figureAt(scope, route, `.${field}`).textContent).toBe(chars.get(`.${field}`));
    }
    expect(scope.textContent).not.toContain("no decision reached");

    // Every boolean but `passed` prints its own name and its own value, verbatim.
    expect(within(scope).getAllByTestId("pass-mark").map(markText)).toEqual([
      "candidate: true",
      "promoted: true",
      "dry_run: false",
      "resumed: false",
    ]);
    expect(within(scope).queryByTestId("no-candidate")).toBeNull();
    expect(figureAt(scope, route, ".cand_id").textContent).toBe(body.cand_id);
    expect(figureAt(scope, route, ".improvement_run_id").textContent).toBe(PROMOTED_ID);

    faithful(container, { [route]: promotedCycle.text });
    capture(DECIDED_HTML, container);
    log.push("/improve/<id>: the recorded steps, the `ScorePair` and the `RuleBadge` with its gloss");
  });
});

// ---- The cycles that reached no decision ------------------------------------------------------------

describe("an undecided cycle is explained absence, never a zero (FR-46, FR-57)", () => {
  it("says `no decision reached` and shows no before or after number", async () => {
    const route = cycleRoute(UNDECIDED_ID);
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(route)]: { status: undecidedCycle.code, text: undecidedCycle.text },
    });
    const { container } = await renderCycle(UNDECIDED_ID);

    const body = JSON.parse(undecidedCycle.text) as Record<string, unknown>;
    const scope = panel(CYCLE_TITLE);
    for (const path of UNDECIDED_FIELDS) {
      expect(body[path.slice(1)]).toBeNull();
      const figure = figureAt(scope, route, path);
      // The promoter's fail-closed `0` never reaches the screen (FR-46, `D-2`).
      expect(figure.textContent).toBe("unknown");
      expect(nullReasonFor(route, path)).toBe("no decision reached");
    }
    expect(scope.textContent).toContain("no decision reached");

    // `rule: ""` is the same fact said by `RuleBadge`, and not an empty outline.
    expect(sentCycle(undecidedCycle.text).rule).toBe("");
    expect(within(scope).queryByTestId("rule-gloss")).toBeNull();
    expect(
      within(scope).getByRole("group", { name: "score before unknown, score after unknown" }),
    ).toBeTruthy();

    // No step was reached, so the list is the sentence saying so and never an empty grid.
    expect(within(scope).getByTestId("steps-empty").textContent).toBe(NO_STEPS);
    expect(within(scope).queryByTestId("steps")).toBeNull();

    // And the reflection produced nothing to measure, which the page says in words.
    expect(within(scope).getByTestId("no-candidate").textContent).toBe(NO_CANDIDATE);
    expect(NO_CANDIDATE).toBe("the reflection produced no candidate");
    // `cand_id` is the empty string here, so the figure is not drawn rather than drawn blank.
    expect(scope.querySelector(`[data-path=".cand_id"]`)).toBeNull();

    faithful(container, { [route]: undecidedCycle.text });
    capture(UNDECIDED_HTML, container);
    log.push("/improve/<id>: six nulls read `unknown` with `no decision reached`, and 0 numbers");
  });

  it("shows a baseline-only cycle's measured score as sent and its unmeasured one as unknown", async () => {
    const route = cycleRoute(BASELINE_ID);
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(route)]: { status: baselineOnly.code, text: baselineOnly.text },
    });
    const { container } = await renderCycle(BASELINE_ID);

    const body = sentCycle(baselineOnly.text);
    expect(body.score_before).toBe(0.8);
    expect(body.score_after).toBeNull();
    const scope = panel(CYCLE_TITLE);

    // A measurement was taken on one side, so it is shown; none was taken on the other.
    expect(figureAt(scope, route, ".score_before").textContent).toBe("0.8");
    expect(figureAt(scope, route, ".score_after").textContent).toBe("unknown");
    expect(
      within(scope).getByRole("group", { name: "score before 0.8, score after unknown" }),
    ).toBeTruthy();
    // The sentence belongs to the absence and not to the column: the measured side carries none.
    expect(figureAt(scope, route, ".score_before").getAttribute("aria-label")).toBeNull();
    expect(figureAt(scope, route, ".score_after").getAttribute("aria-label")).toBe(
      "unknown, no decision reached",
    );

    const steps = within(screen.getByTestId("steps")).getAllByRole("listitem");
    expect(steps).toHaveLength(1);
    expect(figureAt(scope, route, ".steps[0]").textContent).toBe("baseline");
    faithful(container, { [route]: baselineOnly.text });
    log.push("/improve/<id>: a baseline-only cycle shows `score_before` as sent, `score_after` unknown");
  });

  it("shows a dry run that produced a candidate and stopped before the promotion", async () => {
    const route = cycleRoute(DRY_RUN_ID);
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(route)]: { status: dryRun.code, text: dryRun.text },
    });
    const { container } = await renderCycle(DRY_RUN_ID);

    const body = sentCycle(dryRun.text);
    expect(body.candidate).toBe(true);
    expect(body.promoted).toBe(false);
    const scope = panel(CYCLE_TITLE);

    // A candidate was produced, so the page does not say it was not.
    expect(within(scope).queryByTestId("no-candidate")).toBeNull();
    expect(within(scope).getAllByTestId("pass-mark").map(markText)).toEqual([
      "candidate: true",
      "promoted: false",
      "dry_run: false",
      "resumed: false",
    ]);
    // It got as far as the holdout, and the promotion step is simply not in the list.
    expect(body.steps).toEqual(["baseline", "reflect", "candidate", "holdout"]);
    expect(within(screen.getByTestId("steps")).getAllByRole("listitem")).toHaveLength(4);
    // The decision was never taken, so the rule says so and the counts stay unknown.
    expect(scope.textContent).toContain(UNDECIDED);
    expect(figureAt(scope, route, ".pass_count_after").textContent).toBe("unknown");
    // And the two scores it did measure are still the characters the wire spelled.
    expect(figureAt(scope, route, ".score_after").textContent).toBe("1.0");
    faithful(container, { [route]: dryRun.text });
    log.push("/improve/<id>: a dry run shows four steps, `no decision reached` and its two scores");
  });

  it("quotes the gateway's own 404 where no ledger holds the improvement run id", async () => {
    const route = cycleRoute(UNKNOWN_ID);
    expect(route).toBe(unknownCycle.route);
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(route)]: { status: unknownCycle.code, text: unknownCycle.text },
    });
    const { container } = await renderCycle(UNKNOWN_ID);

    const body = JSON.parse(unknownCycle.text) as { error: string; message: string };
    expect(body.error).toBe("SurfaceNotFound");
    const shown = panel(CYCLE_TITLE).textContent ?? "";
    expect(shown).toContain(body.error);
    expect(shown).toContain(body.message);
    expect(screen.getAllByRole("alert").length).toBeGreaterThan(0);
    // Nothing was rendered from a cycle the ledgers never held.
    expect(container.querySelectorAll("[data-figure]")).toHaveLength(0);
    log.push("/improve/<id>: an id no ledger holds is `AbsentState` quoting the gateway's own body");
  });
});

// ---- The temptation both pages answer with a command ------------------------------------------------

describe("neither Improvement page writes anything (`D-8`, FR-57)", () => {
  it("offers the promote command to copy, and no control that would run it", async () => {
    const written: string[] = [];
    vi.stubGlobal("navigator", {
      clipboard: {
        writeText: async (line: string) => {
          written.push(line);
        },
      },
    });
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(LISTING_ROUTE)]: { status: promotions.code, text: promotions.text },
    });
    const { container } = await renderImprove();

    const commands = screen.getAllByTestId("cli-command-text").map((each) => each.textContent);
    expect(commands).toContain(PROMOTE_COMMAND);
    expect(PROMOTE_COMMAND).toBe("harnessed improve promote <cand_id>");
    // The placeholder stays a placeholder: a command with a row's id in it would be this page
    // assembling an id (MUST rule 25).
    expect(PROMOTE_COMMAND).toContain("<cand_id>");
    expect(PROMOTE_COMMAND).not.toContain("cand_4820460a275e0bc8");
    expect(screen.getAllByText(PROMOTE_NOTE).length).toBeGreaterThan(0);

    const copy = screen
      .getAllByRole("button", { name: COPY_LABEL })
      .filter((button) => (button.parentElement?.textContent ?? "").includes(PROMOTE_COMMAND));
    fireEvent.click(copy[0] as HTMLElement);
    expect(written).toEqual([PROMOTE_COMMAND]);

    // Every control on the page is read-only: it refetches, opens, copies or navigates.
    expect(writingControls(container, new Set([COPY_LABEL, "Refresh", "Open", "Menu"]))).toEqual([]);
    // And nothing anywhere would send anything but a `GET` (MUST rule 22).
    expect(container.querySelectorAll("form[method]")).toHaveLength(0);
    log.push("/improve: the promote command is offered to copy, and 0 controls write");
  });

  it("offers the same command on the cycle page, where promoting is most tempting", async () => {
    const route = cycleRoute(DRY_RUN_ID);
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(route)]: { status: dryRun.code, text: dryRun.text },
    });
    const { container } = await renderCycle(DRY_RUN_ID);

    // A dry run is a candidate measured and not promoted: the one screen where a button would be
    // most welcome and is most wrong.
    expect(screen.getByTestId("cli-command-text").textContent).toBe(PROMOTE_COMMAND);
    const command = screen.getByTestId("cli-command");
    expect(command.getAttribute("data-route")).toBeNull();
    expect(command.querySelector("[data-figure]")).toBeNull();

    expect(writingControls(container, new Set([COPY_LABEL, "Refresh", "Menu"]))).toEqual([]);
    log.push("/improve/<id>: the cycle page offers the command and no button that promotes");
  });

  it("shows the cycle's served provider in mono with the Vision's caveat as its gloss", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(LISTING_ROUTE)]: { status: promotions.code, text: promotions.text },
      [proxied(cycleRoute(PROMOTED_ID))]: { status: promotedCycle.code, text: promotedCycle.text },
    });
    await renderImprove();
    // Decision 40's static line is gone from the listing: no eval run renders there to name.
    expect(screen.queryByTestId("provider-caveat")).toBeNull();
    expect(document.body.textContent).not.toContain("does not record which provider");
    cleanup();

    const { container } = await renderCycle(PROMOTED_ID);
    expect(screen.queryByTestId("provider-caveat")).toBeNull();
    const line = screen.getByTestId("provider-line");
    const provider = line.querySelector<HTMLElement>('[data-figure][data-path=".provider"]');
    expect(provider?.textContent).toBe("fake");
    expect(provider?.getAttribute("data-route")).toBe(cycleRoute(PROMOTED_ID));
    const served = JSON.parse(promotedCycle.text) as { model: string };
    expect(line.querySelector('[data-figure][data-path=".model"]')?.textContent).toBe(served.model);
    expect(within(line).getByTestId("provider-gloss").textContent).toBe(FAKE_GLOSS);
    expect(FAKE_GLOSS).toBe("this proves the promotion mechanism, not model quality");
    // And every figure that is a figure says where it came from: 0 without a source (FR-51).
    expect(figuresWithoutSource(container)).toEqual([]);
    log.push("/improve?id=: provider: fake from the served field, glossed; decision 40's line gone");
  });

  it("reads unknown with its migration for a cycle whose baseline predates 010", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(cycleRoute(UNDECIDED_ID))]: {
        status: undecidedCycle.code,
        text: undecidedCycle.text,
      },
    });
    await renderCycle(UNDECIDED_ID);
    const line = screen.getByTestId("provider-line");
    const provider = line.querySelector('[data-figure][data-path=".provider"]');
    expect(provider?.textContent).toBe("unknown");
    expect(line.textContent).toContain("recorded before Harnessed recorded the provider and model");
    expect(within(line).queryByTestId("provider-gloss")).toBeNull();
    log.push("/improve?id=: a cycle with no recorded provider reads unknown, never a default");
  });

  it("is reachable from the narrow menu as well as from the sidebar", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(LISTING_ROUTE)]: { status: promotions.code, text: promotions.text },
    });
    await renderImprove();

    expect(screen.getByRole("navigation").getAttribute("data-open")).toBe("false");
    fireEvent.click(screen.getByRole("button", { name: "Menu" }));
    expect(screen.getByRole("navigation").getAttribute("data-open")).toBe("true");
    expect(
      within(screen.getByRole("navigation")).getByRole("link", { name: "Evals" }),
    ).toBeTruthy();
    log.push("/improve: the page is reachable from the narrow menu and from the sidebar");
  });
});

// ---- The states design.md lets no panel skip --------------------------------------------------------

describe("the Improvement panels wear every state, and remember nothing (design.md)", () => {
  it("is busy before an answer and holds the answer after it", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(LISTING_ROUTE)]: { status: promotions.code, text: promotions.text },
    });
    const page = pageAt(ImproveView, {});
    render(shell(page));

    expect(panel(PROMOTIONS_TITLE).getAttribute("aria-busy")).toBe("true");
    expect(screen.getAllByTestId("skeleton-row").length).toBeGreaterThan(0);
    // The pill has not heard from its own poll yet either, and says so rather than guessing.
    expect(screen.getByTestId("gateway-status-pill").textContent).toContain("checking");

    await settle();
    expect(panel(PROMOTIONS_TITLE).getAttribute("aria-busy")).toBe("false");
    expect(screen.queryAllByTestId("skeleton-row")).toEqual([]);

    const footer = panel(PROMOTIONS_TITLE).querySelector("footer");
    expect(footer?.textContent).toContain("Updated 0 min ago");
    expect(footer?.textContent).not.toContain(LISTING_ROUTE);
    expect(footer?.textContent).not.toContain("200");
    expect(footer?.textContent).not.toContain("fetched");
    log.push("panel: `loading` is the busy skeleton, and the promotion history replaces it");
  });

  // Slow by nature: renders the page twice across a five-minute fake-clock jump; 4.0 s on the operator's PC.
  it("goes stale five minutes after it fetched, and a refetch clears the badge", { timeout: 15_000 }, async () => {
    vi.useFakeTimers();
    const route = cycleRoute(PROMOTED_ID);
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(route)]: { status: promotedCycle.code, text: promotedCycle.text },
    });
    const page = pageAt(ImproveView, { id: PROMOTED_ID });
    render(shell(page));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(screen.getByTestId("stale-badge").textContent).toBe("Updated 0 min ago");
    expect(screen.getByTestId("stale-badge").getAttribute("data-warn")).toBe("false");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5 * 60 * 1000);
    });
    expect(screen.getByTestId("stale-badge").textContent).toBe("Updated 5 min ago");
    expect(screen.getByTestId("stale-badge").getAttribute("data-warn")).toBe("true");
    expect(screen.getByTestId("panel-announcement").textContent).toBe("Updated 5 min ago");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(60 * 1000);
    });
    expect(screen.getByTestId("stale-badge").textContent).toBe("Updated 6 min ago");
    // The announcement is frozen at the crossing, so a screen reader hears it once.
    expect(screen.getByTestId("panel-announcement").textContent).toBe("Updated 5 min ago");

    await act(async () => {
      fireEvent.click(screen.getAllByRole("button", { name: "Refresh" })[0] as HTMLElement);
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(screen.getByTestId("stale-badge").textContent).toBe("Updated 0 min ago");
    expect(screen.getByTestId("stale-badge").getAttribute("data-warn")).toBe("false");
    expect(screen.getByTestId("panel-announcement").textContent).toBe("");
    log.push("panel: `stale` at five minutes, announced once, cleared by `Refresh`");
  });

  it("renders no figure from memory when the gateway cannot be reached", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(LISTING_ROUTE)]: "rejects",
    });
    const { container } = await renderImprove();

    expect(screen.getByRole("heading", { name: "Gateway unreachable" })).toBeTruthy();
    expect(container.querySelectorAll("[data-figure]")).toHaveLength(0);
    // A failed data read never rewrites the pill, which reads its own poll (decision 34).
    expect(screen.getByTestId("gateway-status-pill").getAttribute("data-state")).toBe("reachable");
    cleanup();

    answersWith({ [HEALTH_URL]: HEALTH_OK, [proxied(LISTING_ROUTE)]: "rejects" });
    await renderImprove();
    expect(screen.getByTestId("unreachable-cause").textContent).toBe("ECONNREFUSED");
    cleanup();

    answersWith({ [HEALTH_URL]: HEALTH_OK, [proxied(LISTING_ROUTE)]: "rejects silently" });
    await renderImprove();
    expect(screen.getByTestId("unreachable-cause").textContent).toBe("TypeError");
    cleanup();

    // A refusal that is not an `Error` has no name or message to read, so it is quoted as itself.
    answersWith({ [HEALTH_URL]: HEALTH_OK, [proxied(LISTING_ROUTE)]: "rejects with no error" });
    await renderImprove();
    expect(screen.getByTestId("unreachable-cause").textContent).toBe(
      "the browser refused the request",
    );
    cleanup();

    log.push("panel: an unreachable gateway leaves 0 figures, and names only the cause it gave");
  });

  it("quotes a body that is not the error contract as itself, and offers the read again", async () => {
    const route = cycleRoute(PROMOTED_ID);
    const spy = answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(route)]: { status: 500, text: "the gateway fell over" },
    });
    await renderCycle(PROMOTED_ID);

    const shown = panel(CYCLE_TITLE).textContent ?? "";
    expect(shown).toContain("UnreadableBody");
    expect(shown).toContain("the gateway fell over");
    expect(shown).toContain("500");

    const before = spy.mock.calls.length;
    await act(async () => {
      fireEvent.click(
        within(panel(CYCLE_TITLE)).getAllByRole("button", { name: "Refetch" })[0] as HTMLElement,
      );
    });
    expect(spy.mock.calls.length).toBeGreaterThan(before);
    log.push("panel: a body outside decision 24's contract is quoted as itself, and read again");
  });

  it("lets the pill speak for its own poll while the history renders", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(LISTING_ROUTE)]: { status: promotions.code, text: promotions.text },
    });
    await renderImprove();

    const pill = screen.getByTestId("gateway-status-pill");
    expect(pill.getAttribute("data-state")).toBe("reachable");
    expect(screen.getByTestId("pill-announcement").textContent).toBe("gateway reachable");

    fireEvent.click(pill);
    const dialog = screen.getByRole("dialog");
    // The pill cites no path, so its drawer highlights nothing rather than guessing at one.
    expect(within(dialog).getByTestId("record-view")).toBeTruthy();
    expect(within(dialog).queryByTestId("cited")).toBeNull();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(screen.getByTestId("gateway-status-pill"));
    cleanup();

    answersWith({
      [HEALTH_URL]: "rejects",
      [proxied(LISTING_ROUTE)]: { status: promotions.code, text: promotions.text },
    });
    await renderImprove();
    const down = screen.getByTestId("gateway-status-pill");
    expect(down.getAttribute("data-state")).toBe("unreachable");
    expect((down as HTMLButtonElement).disabled).toBe(true);
    cleanup();

    answersWith({
      [HEALTH_URL]: { status: 500, text: '{"error":"InternalError","message":"it failed"}' },
      [proxied(LISTING_ROUTE)]: { status: promotions.code, text: promotions.text },
    });
    await renderImprove();
    expect(screen.getByTestId("gateway-status-pill").getAttribute("data-state")).toBe("error");
    expect(screen.getByTestId("gateway-status-pill").textContent).toContain("gateway error 500");
    log.push("panel: the pill's four words are its own poll's, never the history's");
  });

  it("renders the same history again from the same URL, remembering nothing", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(LISTING_ROUTE)]: { status: promotions.code, text: promotions.text },
    });
    const opened = await renderImprove();
    const before = renderedFigures(opened.container);
    cleanup();

    const again = answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(LISTING_ROUTE)]: { status: promotions.code, text: promotions.text },
    });
    const reopened = await renderImprove();
    expect(renderedFigures(reopened.container)).toEqual(before);
    expect(again.mock.calls.map((call) => call[0])).toContain(proxied(LISTING_ROUTE));
    log.push("/improve: a reload of the same URL renders the same history (`D-6`)");
  });

  it("renders nothing from a read that came back after the page was left (`D-6`)", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(LISTING_ROUTE)]: { status: promotions.code, text: promotions.text },
    });
    const page = pageAt(ImproveView, {});
    render(shell(page));

    cleanup();
    await settle();
    expect(screen.queryByRole("region", { name: PROMOTIONS_TITLE })).toBeNull();
    expect(document.body.textContent).toBe("");
    log.push("panel: an answer that arrives after the page was left is rendered nowhere (`D-6`)");
  });
});

// ---- Where each figure says it came from, proved against the bytes -----------------------------------

describe("every figure opens the characters it was read out of (FR-51, `D-4`)", () => {
  it("cites the cycle route and a path in its bytes for every figure on the page", async () => {
    const route = cycleRoute(PROMOTED_ID);
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(route)]: { status: promotedCycle.code, text: promotedCycle.text },
    });
    const { container } = await renderCycle(PROMOTED_ID);

    // Every figure the recorded cycle can put on this page, counted: its id, suite and candidate
    // id, its provider and model (FR-72), four booleans, the rule, two scores, five counts and one
    // per step recorded.
    const figures = renderedFigures(container);
    expect(figures).toHaveLength(22);
    expect(figuresWithoutSource(container)).toEqual([]);
    expect([...new Set(figures.map((figure) => figure.route))]).toEqual([route]);

    // And every cited path is a path the raw bytes actually hold: a computed figure has none.
    const chars = sourceCharactersByPath(promotedCycle.text);
    expect(figures.filter((figure) => !chars.has(figure.path))).toEqual([]);
    expect(divergences(figures, new Map([[route, promotedCycle.text]]))).toEqual([]);
    log.push(`/improve/<id>: ${figures.length} figures, every one a leaf of the cycle's response`);
  });

  it("shows no difference figure and no glyph that would carry a direction", async () => {
    const route = cycleRoute(PROMOTED_ID);
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(route)]: { status: promotedCycle.code, text: promotedCycle.text },
    });
    const { container } = await renderCycle(PROMOTED_ID);

    // The cycle sends a before and an after and no difference, so a difference on this page would
    // be one the browser invented (MUST rules 1 and 21). These are what the two scores subtract to.
    const page = container.textContent ?? "";
    for (const invented of ["0.2", "+0.2", "25%", "delta", "Delta", "better", "worse"]) {
      expect(page).not.toContain(invented);
    }
    for (const glyph of ["↑", "↓", "▲", "▼", "→", "⇒"]) {
      expect(page).not.toContain(glyph);
    }
    expect(screen.getByTestId("score-pair-join").textContent).toBe("->");

    const paths = new Set(renderedFigures(container).map((figure) => figure.path));
    for (const path of paths) expect(path).not.toContain("diff");
    log.push("/improve/<id>: 0 difference figures, 0 direction glyphs - `ScorePair` joins with `->`");
  });

  it("opens the cycle's own bytes from every chip, with the gateway's URL and no proxy path", async () => {
    vi.stubGlobal("matchMedia", (query: string) => ({ matches: false, media: query }));
    const route = cycleRoute(PROMOTED_ID);
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(route)]: { status: promotedCycle.code, text: promotedCycle.text },
    });
    await renderCycle(PROMOTED_ID);

    const scope = panel(CYCLE_TITLE);
    const chips = [...scope.querySelectorAll<HTMLElement>("[data-figure]")].filter(
      (chip) => chip.getAttribute("data-route") === route,
    );
    expect(chips).toHaveLength(22);
    for (const chip of chips) {
      // The gateway's own route, the same characters `data-route` carries - never a proxy path.
      expect(chip.getAttribute("data-route")).toBe(route);
      fireEvent.click(chip);
      const dialog = screen.getByRole("dialog");
      expect(within(dialog).getByTestId("record-view")).toBeTruthy();
      expect(within(dialog).getByTestId("cited").textContent).toBeTruthy();
      fireEvent.keyDown(document, { key: "Escape" });
    }

    // Enter opens a chip; nothing else does, so it is not a trap for any other key.
    const first = chips[0] as HTMLElement;
    fireEvent.keyDown(first, { key: "ArrowRight" });
    expect(screen.queryByRole("dialog")).toBeNull();
    fireEvent.keyDown(first, { key: "Enter" });

    const dialog = screen.getByRole("dialog");
    // No endpoint string anywhere in the dialog: not the route, not the origin, not a method.
    expect(within(dialog).getByRole("heading").textContent).toBe("Where this came from");
    expect(dialog.textContent).not.toContain(route);
    expect(dialog.textContent).not.toContain(GATEWAY);
    expect(dialog.textContent).not.toContain("GET");
    // `harnessed improve show` prints this payload since ticket 232, and the drawer offers it.
    expect(within(dialog).getByTestId("cli-twin").textContent).toBe(
      "harnessed improve show <id> --json",
    );

    const controls = within(dialog).getAllByRole("button");
    const head = controls[0] as HTMLElement;
    const tail = controls[controls.length - 1] as HTMLElement;
    tail.focus();
    fireEvent.keyDown(dialog, { key: "Tab" });
    expect(document.activeElement).toBe(head);
    fireEvent.keyDown(dialog, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(tail);
    fireEvent.keyDown(dialog, { key: "ArrowDown" });

    fireEvent.click(within(dialog).getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(first);
    log.push(`/improve/<id>: all ${chips.length} chips cite a span of the cycle's own bytes`);
  });

  it("marks the occurrence the path names, not the first one that looks like it", async () => {
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(LISTING_ROUTE)]: { status: promotions.code, text: promotions.text },
    });
    await renderImprove();

    // Three paths whose characters recur in this body: `0` is in two fields, `0.8` is the first
    // half of the `ScorePair`, and `5` is a pass count that also appears on the other side.
    const pins = [
      { path: ".promotions[0].soft_drops", cited: "0" },
      { path: ".promotions[0].score_before", cited: "0.8" },
      { path: ".promotions[0].passes_after", cited: "5" },
    ];

    for (const pin of pins) {
      const chip = figureAt(panel(PROMOTIONS_TITLE), LISTING_ROUTE, pin.path);
      fireEvent.click(chip);

      const dialog = screen.getByRole("dialog");
      const mark = within(dialog).getByTestId("cited");
      // The occurrence highlighted is the one the path names, structurally - never a look-alike
      // elsewhere in the body, and never the first match a string search would find.
      expect(mark.textContent).toBe(pin.cited);
      expect(mark.getAttribute("aria-current")).toBe("true");
      fireEvent.keyDown(document, { key: "Escape" });
    }
    log.push("/improve: a chip marks the field its path names, never a look-alike earlier on");
  });

  it("opens without motion where the operator asked for none (design.md)", async () => {
    vi.stubGlobal("matchMedia", (query: string) => ({ matches: true, media: query }));
    answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied(LISTING_ROUTE)]: { status: promotions.code, text: promotions.text },
    });
    await renderImprove();

    const chip = figureAt(panel(PROMOTIONS_TITLE), LISTING_ROUTE, ".promotions[0].cand_id");
    fireEvent.click(chip);
    expect(screen.getByRole("dialog").getAttribute("data-motion")).toBe("instant");
    log.push("/improve: a reduced-motion setting is honoured by the provenance drawer");
  });

  it("gives a figure no source to open where no panel fetched one (MUST rule 21)", () => {
    const wire = parseWire(promotedCycle.text, cycleRoute(PROMOTED_ID));
    const loose = render(<Value leaf={wire.at(".score_before")} />);
    expect(loose.container.textContent).toBe("0.8");
    expect(figuresWithoutSource(loose.container)).toEqual([]);
    expect(loose.container.querySelector("button")).toBeNull();
    cleanup();

    // An absent figure is the word and its documented reason, panel or no panel (`D-2`).
    const route = cycleRoute(UNDECIDED_ID);
    const nothing = parseWire(undecidedCycle.text, route);
    const absent = render(
      <Value leaf={nothing.at(".score_after")} nullReason={nullReasonFor(route, ".score_after")} />,
    );
    expect(absent.container.textContent).toContain("unknown");
    expect(absent.container.textContent).toContain("no decision reached");
    expect(absent.container.querySelector("button")).toBeNull();
    cleanup();

    // A null the design documents no reason for still reads `unknown`, and says only that.
    const bare = render(<Value leaf={nothing.at(".score_after")} />);
    expect(bare.container.textContent).toBe("unknown");
    expect(bare.container.querySelector("[data-figure]")?.getAttribute("aria-label")).toBe(
      "unknown",
    );
    log.push("/improve: a figure with no panel behind it offers no source to open");
  });

  it("names the relative origin for the routes these panels read (decision 43)", () => {
    // The origin already carries `/v1`, so a route that spells the version is not made to spell it
    // twice and one that does not is appended.
    expect(gatewayHref(GATEWAY, LISTING_ROUTE)).toBe(`${GATEWAY}/promotions?limit=20`);
    expect(gatewayHref(GATEWAY, cycleRoute(PROMOTED_ID))).toBe(
      `${GATEWAY}/improve/cycles/${PROMOTED_ID}`,
    );
    expect(gatewayHref(GATEWAY, "/promotions")).toBe(`${GATEWAY}/promotions`);
    log.push("both pages: the `curl` line names the gateway once, at the relative `/v1` origin");
  });

  it("keys its documented nulls on the cycle route alone, and claims no CLI twin", () => {
    // FR-46's six, and nothing else. A `scored` of 0 is a count, so asking for a reason beside one
    // would print "no decision reached" next to a number the gateway did send (ticket 104).
    for (const path of UNDECIDED_FIELDS) {
      expect(nullReasonFor(cycleRoute(PROMOTED_ID), path)).toBe("no decision reached");
    }
    expect(nullReasonFor(cycleRoute(PROMOTED_ID), ".scored")).toBeUndefined();
    expect(nullReasonFor(cycleRoute(PROMOTED_ID), ".rule")).toBeUndefined();
    // The promotions listing documents no null at all: every field of a decided row is a figure.
    expect(nullReasonFor(LISTING_ROUTE, ".promotions[].score_before")).toBeUndefined();

    // Both routes have a `--json` twin since ticket 232, so the drawer offers each.
    expect(cliTwinFor(cycleRoute(PROMOTED_ID))).toBe("harnessed improve show <id> --json");
    expect(cliTwinFor(LISTING_ROUTE)).toBe("harnessed improve promotions --json");

    // A 404 and zero rows on these routes mean what the gateway's own words say, and nothing more.
    expect(absentTextFor(cycleRoute(PROMOTED_ID))).toBeUndefined();
    expect(emptyTextFor(LISTING_ROUTE)).toBeUndefined();
    log.push("both pages: six documented nulls on the cycle route, none on the listing, no twin");
  });
});

// ---- Evidence ----------------------------------------------------------------------------------------

describe("evidence", () => {
  it("writes the manifest this ticket declares", () => {
    expect(log.length).toBeGreaterThan(10);
    mkdirSync(dirname(EVIDENCE), { recursive: true });
    writeFileSync(EVIDENCE, `${log.join("\n")}\n`, "utf8");
  });
});

});

const MEASURED_ID_207 = "eval_0000000005AVYXNCY6ZK6K3CP9";

describe("/evals holds the eval runs, Compare and Improvement, and the old pages forward (ticket 207)", () => {
  const fixtures = resolve(import.meta.dirname, "../fixtures/gateway");
  function recorded(name: string): { code: number; text: string } {
    const meta = JSON.parse(readFileSync(resolve(fixtures, `${name}.meta.json`), "utf8")) as { status: number };
    return { code: meta.status, text: readFileSync(resolve(fixtures, `${name}.body`), "utf8") };
  }
  const health207 = recorded("health");
  const HEALTH_URL = "/v1/health";
  const HEALTH_OK = { status: health207.code, text: health207.text };
  const proxied = (route: string): string => route;
  type Route = { status: number; text: string };

  function answersWith(routes: Record<string, Route>): ReturnType<typeof vi.fn> {
    const spy = vi.fn(async (url: string) => {
      const next = routes[url];
      if (next === undefined) throw new Error(`no answer stubbed for ${url}`);
      return {
        status: next.status,
        ok: next.status === 200,
        headers: { get: () => null },
        text: async () => next.text,
      } as unknown as Response;
    });
    vi.stubGlobal("fetch", spy);
    return spy;
  }

  const settle = async (): Promise<void> => {
    await act(async () => {});
  };
  const shell = (children: ReactNode) => <AppShell>{children}</AppShell>;

  beforeEach(() => {
    pushed.length = 0;
    vi.stubGlobal("matchMedia", (query: string) => ({ matches: false, media: query }));
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  const evalRuns = recorded("eval.runs.listing");
  const promos = recorded("promotions");
  const run = recorded("eval.run.measured");
  const cyc = recorded("improve.cycle.promoted");
  const bare = () => ({
    [HEALTH_URL]: HEALTH_OK,
    [proxied("/v1/eval/runs?limit=20")]: { status: evalRuns.code, text: evalRuns.text },
    [proxied("/v1/promotions?limit=20")]: { status: promos.code, text: promos.text },
  });

  async function open(params: Record<string, string>) {
    render(shell(pageAt(EvalsPage, params)));
    await settle();
  }

  it("lists 6 pages in the nav and 0 named Compare or Improvement", async () => {
    answersWith(bare());
    await open({});
    const labels = within(screen.getByRole("navigation"))
      .getAllByRole("link")
      .map((link) => link.textContent);
    expect(labels).toEqual(["Overview", "Console", "Evals", "Memory", "Traces", "Models"]);
    expect(labels.filter((label) => label === "Compare" || label === "Improvement")).toHaveLength(0);
  });

  it("stacks Eval runs, Compare and Improvement in that order, one ?limit= driving both listings", async () => {
    const spy = answersWith({
      [HEALTH_URL]: HEALTH_OK,
      [proxied("/v1/eval/runs?limit=50")]: { status: evalRuns.code, text: evalRuns.text },
      [proxied("/v1/promotions?limit=50")]: { status: promos.code, text: promos.text },
    });
    await open({ limit: "50" });
    const asked = spy.mock.calls.map((call) => call[0] as string);
    expect(asked).toContain(proxied("/v1/eval/runs?limit=50"));
    expect(asked).toContain(proxied("/v1/promotions?limit=50"));
    expect(screen.getAllByTestId("limit-control")).toHaveLength(1);

    const main = screen.getByRole("main");
    const runs = main.querySelector('[data-testid="evals-footer"]') as HTMLElement;
    const compare = main.querySelector("#compare") as HTMLElement;
    const improvement = main.querySelector("#improvement") as HTMLElement;
    expect(compare).toBeTruthy();
    expect(improvement).toBeTruthy();
    const before = Node.DOCUMENT_POSITION_FOLLOWING;
    expect(runs.compareDocumentPosition(compare) & before).toBeTruthy();
    expect(compare.compareDocumentPosition(improvement) & before).toBeTruthy();
    expect(within(compare).getAllByLabelText("eval run id")).toHaveLength(2);
    expect(within(improvement).getByTestId("promotions-footer")).toBeTruthy();
    expect(within(improvement).getAllByTestId("cli-command")).toHaveLength(2);
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Evals");
  });

  it("?before=&after= fills Compare with the comparison", async () => {
    const regression = recorded("eval.compare.regression");
    const spy = answersWith({
      ...bare(),
      [proxied("/v1/eval/compare?before=eval_recorded_before&after=eval_recorded_after")]: {
        status: regression.code,
        text: regression.text,
      },
    });
    await open({ before: "eval_recorded_before", after: "eval_recorded_after" });
    const asked = spy.mock.calls.map((call) => call[0] as string);
    expect(asked).toContain(proxied("/v1/eval/compare?before=eval_recorded_before&after=eval_recorded_after"));
    expect(within(document.querySelector("#compare") as HTMLElement).getByRole("table", { name: /compared/ })).toBeTruthy();
  });

  it("?id= puts the eval run panel at the top, and ?cycle= puts the cycle in Improvement", async () => {
    answersWith({ ...bare(), [proxied(`/v1/eval/runs/${MEASURED_ID_207}`)]: { status: run.code, text: run.text } });
    await open({ id: MEASURED_ID_207 });
    const first = screen.getByRole("main").querySelector("section, [data-panel]") as HTMLElement;
    expect(first).toBeTruthy();
    cleanup();

    answersWith({ ...bare(), [proxied("/v1/improve/cycles/imp_X")]: { status: cyc.code, text: cyc.text } });
    await open({ cycle: "imp_X" });
    expect(within(document.querySelector("#improvement") as HTMLElement).getByText(CYCLE_TITLE)).toBeTruthy();
  });

  it("points the improve box, cycle lookup and pick-two link at /evals", () => {
    expect(cycleHref("imp_A b")).toBe("/evals?cycle=imp_A%20b");
    expect(compareHref("a", "b")).toBe("/evals?before=a&after=b");
    expect(sideHref("before", "a", undefined)).toBe("/evals?before=a");
  });

  it.each([
    [CompareForward, { before: "A", after: "B" }, "/evals?before=A&after=B"],
    [ImproveForward, { id: "C" }, "/evals?cycle=C"],
    [ImproveForward, { limit: "50" }, "/evals?limit=50"],
  ])("forwards an old URL to %#: %j -> %s", (Page, params, to) => {
    pushed.length = 0;
    render(pageAt(Page, params));
    expect(screen.getByRole("link", { name: "Moved to Evals" }).getAttribute("href")).toBe(to);
    expect(pushed).toEqual([to]);
  });
});

describe("the Evals page lists work suites and their verdict coverage (ticket 238)", () => {
  const fixtures = resolve(import.meta.dirname, "../fixtures/gateway");
  function recorded(name: string): { code: number; text: string } {
    const meta = JSON.parse(readFileSync(resolve(fixtures, `${name}.meta.json`), "utf8")) as { status: number };
    return { code: meta.status, text: readFileSync(resolve(fixtures, `${name}.body`), "utf8") };
  }
  const suites = recorded("eval.suites.listing");
  const listing = recorded("eval.runs.listing");
  const health = recorded("health");
  const HEALTH_URL = "/v1/health";
  const HEALTH_OK = { status: health.code, text: health.text };
  const SUITES_URL = "/v1/eval/suites";
  const proxied = (route: string): string => route;
  const listingRoute = (limit: string): string => `/v1/eval/runs?limit=${limit}`;
  type Route = { status: number; text: string };

  function answersWith(table: Record<string, Route>): ReturnType<typeof vi.fn> {
    const spy = vi.fn(async (url: string) => {
      const next = table[url];
      if (next === undefined) throw new Error(`no answer stubbed for ${url}`);
      return {
        status: next.status,
        ok: next.status === 200,
        headers: { get: () => null },
        text: async () => next.text,
      } as unknown as Response;
    });
    vi.stubGlobal("fetch", spy);
    return spy;
  }

  const settle = async (): Promise<void> => {
    await act(async () => {});
  };
  const shell = (children: ReactNode) => <AppShell>{children}</AppShell>;
  const panel = (title: string): HTMLElement => screen.getByRole("region", { name: title });

  /** One suite as `/v1/eval/suites` serves it, the `latest_*` fields given or null. */
  function suiteBody(
    rows: { suite: string; kind: string; total: string; pending: string; run?: string; score?: string }[],
  ): string {
    const served = rows.map((row) =>
      [
        `{"cases_pending":"${row.pending}","cases_total":"${row.total}","description":"d","kind":"${row.kind}",`,
        `"latest_eval_run_id":${row.run === undefined ? "null" : `"${row.run}"`},`,
        `"latest_label":${row.run === undefined ? "null" : '"baseline"'},`,
        `"latest_score":${row.score ?? "null"},`,
        `"latest_started_at":${row.run === undefined ? "null" : '"2026-01-01T12:00:00Z"'},`,
        `"suite":"${row.suite}"}`,
      ].join(""),
    );
    return `{"suites":[${served.join(",")}]}`;
  }

  function routes(suitesText: string): Record<string, Route> {
    return {
      [HEALTH_URL]: HEALTH_OK,
      [proxied(listingRoute("20"))]: { status: listing.code, text: listing.text },
      [proxied("/v1/promotions?limit=20")]: { status: 200, text: '{"promotions":[]}' },
      [SUITES_URL]: { status: 200, text: suitesText },
    };
  }

  async function openEvals() {
    render(shell(pageAt(EvalsPage, {})));
    await settle();
  }

  beforeEach(() => {
    vi.stubGlobal("matchMedia", (query: string) => ({ matches: false, media: query }));
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("draws a WorkSuiteRow per work suite under a Work suites heading above Eval runs, and none for smoke", async () => {
    answersWith(routes(suites.text));
    await openEvals();
    const work = panel("Work suites");
    const rows = within(work).getAllByTestId("work-suite-row");
    expect(rows.map((row) => row.getAttribute("aria-label"))).toEqual(["work-notes", "work-reports"]);
    expect(screen.queryByRole("group", { name: "smoke" })).toBeNull();
    const runs = panel("Eval runs");
    expect(work.compareDocumentPosition(runs) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("says how many cases await a verdict and offers the pending command with the served name", async () => {
    answersWith(routes(suites.text));
    await openEvals();
    const pending = screen.getByRole("listitem", { name: "work-notes" });
    expect(pending.textContent).toContain("2 cases, 1 awaiting a verdict");
    const commands = within(pending).getAllByTestId("cli-command-text").map((c) => c.textContent ?? "");
    expect(commands).toEqual(["harnessed eval run work-notes", "harnessed eval verdict work-notes --pending"]);
    for (const command of commands) expect(command).not.toContain("<");
    expect(within(pending).getAllByRole("button", { name: COPY_LABEL })).toHaveLength(2);

    const complete = screen.getByRole("listitem", { name: "work-reports" });
    expect(complete.textContent).toContain("1 cases, all with a verdict");
    expect(within(complete).getAllByTestId("cli-command-text").map((c) => c.textContent)).toEqual([
      "harnessed eval run work-reports",
    ]);
  });

  it("offers the eval run line for work-email even with every verdict in", async () => {
    const body = suiteBody([{ suite: "work-email", kind: "work", total: "3", pending: "0" }]);
    answersWith(routes(body));
    await openEvals();
    const row = screen.getByRole("listitem", { name: "work-email" });
    const lines = within(row).getAllByTestId("cli-command-text");
    expect(lines.map((c) => c.textContent)).toEqual(["harnessed eval run work-email"]);
    expect(within(row).getAllByTestId("cli-command-text")[0]?.textContent).not.toContain("<");
  });

  it("reads an eval run's started time in words, nowrap, with the served stamp in the tooltip", async () => {
    answersWith(routes(suites.text));
    await openEvals();
    const table = within(panel("Eval runs")).getByRole("table");
    const row = within(table).getAllByRole("row")[1] as HTMLElement;
    const cell = within(row).getAllByRole("cell")[3] as HTMLElement;
    expect(cell.textContent).not.toMatch(/\d{4}-\d{2}-\d{2}T/);
    expect(cell.textContent).toMatch(/ago|\d{2}:\d{2}/);
    const stamp = JSON.parse(listing.text).runs[0].started_at as string;
    expect(cell.innerHTML).toContain(stamp);
    expect(cell.querySelector('[class*="nowrap"]')).not.toBeNull();
  });

  it("reads never run where no run is served, and links the served score to its eval run", async () => {
    const body = suiteBody([
      { suite: "work-a", kind: "work", total: "3", pending: "0", run: "eval_planted", score: "0.75" },
      { suite: "work-b", kind: "work", total: "3", pending: "3" },
    ]);
    answersWith(routes(body));
    await openEvals();
    const ran = screen.getByRole("listitem", { name: "work-a" });
    const link = within(ran).getByRole("link", { name: "0.75" });
    expect(link.getAttribute("href")).toBe(evalRunHref("eval_planted"));
    const never = screen.getByRole("listitem", { name: "work-b" });
    expect(within(never).queryByRole("link")).toBeNull();
    expect(never.textContent).toContain("unknown");
    expect(never.textContent).toContain("never run");
  });

  it("draws no Work suites heading when no suite has kind work", async () => {
    answersWith(routes(suiteBody([{ suite: "smoke", kind: "smoke", total: "5", pending: "0" }])));
    await openEvals();
    expect(screen.queryByRole("region", { name: "Work suites" })).toBeNull();
    expect(screen.queryByText("Work suites")).toBeNull();
    expect(panel("Eval runs")).toBeTruthy();
  });

  it("names the twin of the route", () => {
    expect(cliTwinFor(SUITES_URL)).toBe("harnessed eval suites --json");
  });
});
