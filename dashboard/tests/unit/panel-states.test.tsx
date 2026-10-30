// @vitest-environment jsdom
/**
 * FR-50: unknown is not zero, absent is not empty, and a panel has a rendering for every state it
 * can reach (`D-2`, `D-3`, `D-5`, MUST rules 21 and 23; decisions 29, 34).
 *
 * Four layers, each able to fail on its own:
 *   - `lib/null-reasons.ts` holds the sentences `ux/design.md` documents, keyed on route and path;
 *   - `Value` renders the `unknown` treatment for every `null` leaf, never `0`, `-` or blank, and
 *     the sweep below proves that over the whole recorded corpus rather than over a sample;
 *   - `lib/fetch-panel.ts` turns one response into one state, telling unreachable from error by
 *     the transport header and never by the status;
 *   - `PanelFrame` renders exactly one state and provides its raw response to its children.
 *
 * The inputs are the recorded gateway responses (decision 33) plus the one hand-written body the
 * gateway cannot produce, `transport-unreachable`, which the dashboard's own route handler writes.
 */
import { mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AbsentState } from "../../components/AbsentState";
import { EmptyState } from "../../components/EmptyState";
import { ErrorState } from "../../components/ErrorState";
import { PanelFrame, usePanel } from "../../components/PanelFrame";
import { WithRefresh } from "../support/with-refresh";
import { Value } from "../../components/Value";
import { clockTime } from "../../lib/clock";
import { GATEWAY_ORIGIN, fetchPanel } from "../../lib/fetch-panel";
import { absentTextFor, emptyTextFor, nullReasonFor } from "../../lib/null-reasons";
import { parseWire, type Wire } from "../../lib/wire";

const DASHBOARD = resolve(import.meta.dirname, "../..");
const GATEWAY_FIXTURES = resolve(DASHBOARD, "tests/fixtures/gateway");
const EVIDENCE = resolve(DASHBOARD, "../evidence/101/panel-states.txt");

/** The dashboard's clock, frozen so "fetched hh:mm:ss" is the same characters on every run. */
const FROZEN = new Date("2026-09-17T14:03:09");

/** What the operator sees where the gateway sent nothing. Never `0`, never `-`, never blank. */
const UNKNOWN = "unknown";

/** The lines this run writes to `evidence/101/panel-states.txt`. */
const log: string[] = [];

type Recorded = { name: string; route: string; status: string; text: string };

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

/**
 * The jq path of every `null` in a body, found by a walk written here rather than through
 * `lib/wire.ts`: a wire-layer bug that lost a null must show up as a missing case, not as the
 * sweep agreeing with the code it is checking (MUST rule 12).
 */
function nullPaths(text: string): string[] {
  const found: string[] = [];
  const walk = (node: unknown, path: string): void => {
    if (node === null) {
      found.push(path);
      return;
    }
    if (Array.isArray(node)) {
      for (const [index, item] of node.entries()) walk(item, `${path}[${index}]`);
      return;
    }
    if (typeof node === "object") {
      for (const [key, item] of Object.entries(node as Record<string, unknown>)) {
        walk(item, `${path}.${key}`);
      }
    }
  };
  walk(JSON.parse(text), "");
  return found;
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

/** Stub `fetch` with a rejection: the browser could not reach the dashboard's own server. */
function rejectsWith(thrown: Error): ReturnType<typeof vi.fn> {
  const spy = vi.fn(async () => {
    throw thrown;
  });
  vi.stubGlobal("fetch", spy);
  return spy;
}

/** Stub `fetch` with a response whose headers arrive and whose body read then fails. */
function readsFailWith(thrown: Error): ReturnType<typeof vi.fn> {
  const spy = vi.fn(
    async () =>
      ({
        status: 200,
        ok: true,
        headers: { get: () => null },
        text: async () => {
          throw thrown;
        },
      }) as unknown as Response,
  );
  vi.stubGlobal("fetch", spy);
  return spy;
}

const SOURCE_LABEL = "Ledger: all time";

/** A panel over one recorded response, rendering one figure so `ok` has something to show. */
function panelOver(body: Recorded, path: string, extra: { emptyText?: string; cliTwin?: string } = {}) {
  const children = (wire: Wire) => <Value leaf={wire.at(path)} />;
  return (
    <WithRefresh>
      <PanelFrame route={body.route} title="Panel" sourceLabel={SOURCE_LABEL} {...extra}>
        {children}
      </PanelFrame>
    </WithRefresh>
  );
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(FROZEN);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

// ---- `D-2`: unknown is not zero ---------------------------------------------------------------

/**
 * The two nulls the recorded corpus cannot hold: the seeded home is newer than migrations 006 and
 * 007, so every recorded turn carries both columns. Their reasons are documented copy all the
 * same, so they are rendered here from a body written in this file rather than left untested.
 */
const OLD_TURN = '{"stop_reason":null,"usage":null}';
const RUN_ROUTE = "/v1/runs/run_0000000002NEHPN093CBQMNNV3";

describe("the unknown treatment (D-2)", () => {
  it("renders a null as `unknown` with the reason design.md documents, never as 0", () => {
    const wire = parseWire(OLD_TURN, RUN_ROUTE);
    const usage = wire.at(".usage");
    render(<Value leaf={usage} nullReason={nullReasonFor(RUN_ROUTE, ".usage")} />);

    expect(usage.text).toBe("null");
    expect(screen.getByText(UNKNOWN).textContent).toBe(UNKNOWN);
    expect(
      screen.getByText("recorded before Harnessed measured this, so a partial sum would understate the run"),
    ).toBeTruthy();
    expect(screen.queryByText("0")).toBeNull();
    expect(screen.queryByText("-")).toBeNull();
    expect(screen.queryByText("null")).toBeNull();
  });

  it("announces `unknown` with its reason, and keeps the figure's route and path", () => {
    const wire = parseWire(OLD_TURN, RUN_ROUTE);
    const reason = nullReasonFor(RUN_ROUTE, ".stop_reason");
    render(<Value leaf={wire.at(".stop_reason")} nullReason={reason} />);

    const figure = screen.getByText(UNKNOWN);
    expect(figure.getAttribute("aria-label")).toBe(`${UNKNOWN}, ${reason}`);
    expect(figure.getAttribute("data-route")).toBe(RUN_ROUTE);
    expect(figure.getAttribute("data-path")).toBe(".stop_reason");
    // No panel around it: nothing to open, and no route in a title (D-9).
    expect(figure.getAttribute("title")).toBeNull();
  });

  it("still says `unknown` where no reason is documented, rather than falling back to a value", () => {
    const wire = parseWire('{"x":null}', "/v1/health");
    render(<Value leaf={wire.at(".x")} nullReason={nullReasonFor("/v1/health", ".x")} />);

    expect(screen.getByText(UNKNOWN).getAttribute("aria-label")).toBe(UNKNOWN);
  });

  it("carries every documented reason, keyed on the route and the jq path", () => {
    const zero = recorded("eval.run.zero-case");
    const listing = recorded("eval.runs.listing");
    const cycle = recorded("improve.cycle.undecided");
    const trace = recorded("runs.trace");

    expect(nullReasonFor(RUN_ROUTE, ".stop_reason")).toBe(
      "recorded before Harnessed measured this",
    );
    expect(nullReasonFor(trace.route, "[0].retrieval")).toBe(
      "the gateway does not pass retrieval to the trace",
    );
    expect(nullReasonFor(zero.route, ".score")).toBe("no evidence: 0 cases");
    expect(nullReasonFor(listing.route, ".runs[3].score")).toBe("no evidence: 0 cases");
    for (const path of [
      ".score_before",
      ".score_after",
      ".regressions",
      ".soft_drops",
      ".pass_count_before",
      ".pass_count_after",
    ]) {
      expect(nullReasonFor(cycle.route, path)).toBe("no decision reached");
    }

    // A route with no documented null gets no invented sentence, and neither does a near miss.
    expect(nullReasonFor("/v1/health", ".schema_version")).toBeUndefined();
    expect(nullReasonFor("/v1/eval/runs", ".score")).toBeUndefined();
    expect(nullReasonFor("/v1/eval", ".score")).toBeUndefined();
  });

  it("renders 100% of the recorded nulls as unknown, and 0 of them as 0, `-` or blank", () => {
    const bodies = everyRecordedBody();
    const walked: string[] = [];

    const cases = bodies.flatMap((body) => {
      const wire = parseWire(body.text, body.route);
      return nullPaths(body.text).map((path) => ({
        body, path, leaf: wire.at(path), reason: nullReasonFor(body.route, path),
      }));
    });
    // Mount once while retaining all per-value assertions.
    const mounted = render(<>{cases.map(({ body, path, leaf, reason }) => (
      <div key={`${body.name} ${path}`}><Value leaf={leaf} nullReason={reason} /></div>
    ))}</>);
    for (const [index, { body, path, leaf, reason }] of cases.entries()) {
      expect(leaf.kind).toBe("null");
      expect(reason, `${body.name} ${path} has no documented reason`).toBeTruthy();
      const container = mounted.container.children[index];
      const figure = container.querySelector(`[data-path="${path}"]`);
      expect(figure?.textContent).toBe(UNKNOWN);
      expect(figure?.getAttribute("data-route")).toBe(body.route);
      expect(container.textContent).toContain(reason);
      for (const forbidden of ["0", "-", "0.0", "null", ""]) {
        expect(figure?.textContent).not.toBe(forbidden);
      }
      walked.push(`${body.name} ${path}`);
    }
    mounted.unmount();

    expect(walked.length).toBeGreaterThan(10);
    expect(walked).toContain("eval.run.zero-case .score");
    expect(walked).toContain("runs.trace [0].retrieval");
    log.push(`D-2: ${walked.length} recorded nulls over ${bodies.length} bodies, all unknown`);
  });
});

// ---- `D-3`: absent is not empty ---------------------------------------------------------------

describe("absent is not empty (D-3)", () => {
  it("quotes the gateway's error and message verbatim in an alert", () => {
    const absent = recorded("notfound.memory.episodes");
    const wire = parseWire(absent.text, absent.route);
    render(
      <AbsentState
        error={wire.at(".error").text}
        message={wire.at(".message").text}
        absentText={absentTextFor(absent.route)}
      />,
    );

    const alert = screen.getByRole("alert");
    expect(alert.textContent).toContain("SurfaceNotFound");
    expect(alert.textContent).toContain(wire.at(".message").text);
    expect(alert.textContent).toContain("404");
  });

  it("never shares a rendering with the empty state", () => {
    const absent = recorded("notfound.memory.episodes");
    const wire = parseWire(absent.text, absent.route);

    const first = render(
      <AbsentState
        error={wire.at(".error").text}
        message={wire.at(".message").text}
        absentText={absentTextFor(absent.route)}
      />,
    );
    const absentText = first.container.textContent ?? "";
    cleanup();

    const empty = recorded("empty.memory.facts");
    const second = render(
      <EmptyState title="Facts" emptyText={emptyTextFor(empty.route)} cliTwin="harnessed memory add" />,
    );
    const emptyText = second.container.textContent ?? "";

    expect(second.container.querySelector('[role="alert"]')).toBeNull();
    expect(emptyText).toContain("No facts recorded yet.");
    expect(emptyText).not.toContain(empty.route);
    expect(emptyText).toContain("harnessed memory add");
    expect(emptyText).not.toContain("404");
    expect(absentText).not.toContain("recorded yet");
    expect(absentText).not.toBe(emptyText);
    log.push("D-3: absent quotes the 404 body, empty names the noun and the CLI command");
  });

  it("says two different sentences for an episodes 404 and an empty episodes list", async () => {
    const absent = recorded("notfound.memory.episodes");
    answersWith({ status: 404, text: absent.text });
    const missing = render(panelOver(absent, ".error"));
    await screen.findByRole("alert");
    const said = missing.container.textContent ?? "";
    cleanup();

    // The gateway answers this shape for a session that wrote no episode; the recorded home has
    // no such session, so the zero-row body is spelled here rather than added to the record.
    const present = recorded("memory.episodes");
    answersWith({ status: 200, text: '{"episodes":[]}' });
    const wrote = render(
      <PanelFrame route={present.route} title="Episodes" sourceLabel={SOURCE_LABEL} cliTwin="harnessed run">
        {() => null}
      </PanelFrame>,
    );
    await screen.findByText(/recorded yet/);
    const other = wrote.container.textContent ?? "";

    expect(said).toContain("the events table never held this session id");
    expect(other).toContain("the session exists and wrote no episode");
    expect(said).not.toContain("the session exists and wrote no episode");
    expect(other).not.toContain("the events table never held this session id");
    log.push("D-3: episodes 404 and episodes 200-empty say two different sentences");
  });
});

// ---- MUST rule 23: unreachable is a header, never a status ------------------------------------

describe("unreachable is a rejected fetch, never a status or a header (MUST rule 23)", () => {
  const transport = recorded("transport-unreachable");

  it("renders ErrorState for a 502 whatever header it carries: no header means anything", async () => {
    answersWith({
      status: 502,
      text: transport.text,
      headers: { "x-anything": "unreachable" },
    });
    render(panelOver(recorded("runs.listing"), ".runs[0].run_key"));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("502");
    expect(alert.textContent).toContain("GatewayUnreachable");
    expect(screen.queryByRole("heading", { name: "Gateway unreachable" })).toBeNull();
  });

  it("renders UnreachableState when the browser fetch itself rejects", async () => {
    rejectsWith(new TypeError("Failed to fetch"));
    render(panelOver(recorded("runs.listing"), ".runs[0].run_key"));

    expect(await screen.findByRole("heading", { name: "Gateway unreachable" })).toBeTruthy();
    expect(screen.getByTestId("unreachable-cause").textContent).toBe("Failed to fetch");
  });

  it("renders UnreachableState when the body read fails after the headers arrived", async () => {
    readsFailWith(new TypeError("network error"));
    render(panelOver(recorded("runs.listing"), ".runs[0].run_key"));

    // A gateway that dies mid-answer is the same silence as one that never answered, and it must
    // be a state: uncaught, this rejection left the panel on its skeleton with no `Refetch`.
    expect(await screen.findByRole("heading", { name: "Gateway unreachable" })).toBeTruthy();
    expect(screen.getByTestId("unreachable-cause").textContent).toBe("network error");
    log.push("MUST 23: a read that fails mid-body is the state a rejected fetch is");
  });

  it("renders ErrorState for a 502 without the header: status alone never decides", async () => {
    answersWith({ status: 502, text: transport.text });
    render(panelOver(recorded("runs.listing"), ".runs[0].run_key"));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("502");
    expect(screen.queryByRole("heading", { name: "Gateway unreachable" })).toBeNull();
    log.push("MUST 23: a 502 is ErrorState with or without a header; only a rejection is unreachable");
  });

  it("renders ErrorState with status, error class, message and a Refetch button for a 400", async () => {
    const bad = recorded("badrequest.runs.limit");
    const wire = parseWire(bad.text, bad.route);
    answersWith({ status: 400, text: bad.text });
    render(panelOver(bad, ".error"));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("400");
    expect(alert.textContent).toContain(wire.at(".error").text);
    expect(alert.textContent).toContain(wire.at(".message").text);
    expect(screen.getAllByRole("button", { name: "Refetch" }).length).toBeGreaterThan(0);
  });

  it("renders ErrorState for a 500, quoting the store's own message", async () => {
    const broken = recorded("nodatabase.runs.listing");
    const wire = parseWire(broken.text, broken.route);
    answersWith({ status: 500, text: broken.text });
    render(panelOver(broken, ".error"));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("500");
    expect(alert.textContent).toContain(wire.at(".error").text);
    expect(alert.textContent).toContain(wire.at(".message").text);
  });
});

// ---- `lib/fetch-panel.ts` ---------------------------------------------------------------------

describe("fetch-panel", () => {
  it("fetches the relative /v1/... with cache: no-store and keeps the bytes, status and URL", async () => {
    const body = recorded("eval.run.zero-case");
    const spy = answersWith({ status: 200, text: body.text });

    const result = await fetchPanel(body.route, GATEWAY_ORIGIN);

    expect(spy).toHaveBeenCalledWith(body.route, expect.objectContaining({ cache: "no-store" }));
    expect(result.state).toBe("ok");
    if (result.state !== "ok") return;
    expect(result.response.text).toBe(body.text);
    expect(result.response.status).toBe("200");
    expect(result.response.bytes).toBe(String(new TextEncoder().encode(body.text).length));
    expect(result.response.url).toBe(
      `${GATEWAY_ORIGIN}/eval/runs/eval_recorded_zero`,
    );
    expect(result.wire.at(".score").text).toBe("null");
  });

  it("takes the fetch time from the dashboard clock on receipt", async () => {
    const body = recorded("health");
    answersWith({ status: 200, text: body.text });

    const result = await fetchPanel(body.route, GATEWAY_ORIGIN);

    expect(result.state).toBe("ok");
    if (result.state !== "ok") return;
    expect(result.response.fetchedAt.toISOString()).toBe(FROZEN.toISOString());
    expect(clockTime(result.response.fetchedAt)).toBe("14:03:09");
    expect(body.text).not.toContain("14:03:09");
  });


  it("names the path a response does not hold rather than answering with a guess", async () => {
    const body = recorded("health");
    answersWith({ status: 200, text: body.text });

    const result = await fetchPanel(body.route, GATEWAY_ORIGIN);
    expect(result.state).toBe("ok");
    if (result.state !== "ok") return;
    expect(() => result.wire.at(".nope")).toThrow(".nope");
  });

  it("quotes a body that is not the error contract's shape rather than inventing one", async () => {
    answersWith({ status: 500, text: "<html>gateway said nothing useful</html>" });

    const result = await fetchPanel("/v1/runs", GATEWAY_ORIGIN);

    expect(result.state).toBe("error");
    if (result.state !== "error") return;
    expect(result.message).toBe("<html>gateway said nothing useful</html>");
  });

  it("turns a 2xx whose body is not JSON into a state, never a rejection", async () => {
    const html = "<html>a proxy's page, served with 200</html>";
    answersWith({ status: 200, text: html });
    render(panelOver(recorded("runs.listing"), ".runs[0].run_key"));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("200");
    expect(alert.textContent).toContain("UnreadableBody");
    expect(alert.textContent).toContain(html);
    log.push("a 200 that is not JSON renders ErrorState; no panel is left loading");
  });

  it("renders ErrorState naming WireUnsupported when the parser cannot show source text", async () => {
    const plain = JSON.parse.bind(JSON);
    const spy = vi.spyOn(JSON, "parse").mockImplementation((text: string, reviver?: unknown) => {
      const twoArg = reviver as ((key: string, value: unknown) => unknown) | undefined;
      // The pre-2025 parser: a reviver, but no third `context` argument to read `source` from.
      return twoArg ? plain(text, (key: string, value: unknown) => twoArg(key, value)) : plain(text);
    });

    vi.resetModules();
    const downgraded = await import("../../lib/fetch-panel");
    const body = recorded("health");
    answersWith({ status: 200, text: body.text });

    const result = await downgraded.fetchPanel(body.route, GATEWAY_ORIGIN);
    expect(result.state).toBe("error");
    if (result.state !== "error") return;
    expect(result.error).toBe("WireUnsupported");

    spy.mockRestore();
    vi.resetModules();

    render(<ErrorState status="200" error={result.error} message={result.message} onRefetch={() => {}} />);
    const alert = screen.getByRole("alert");
    expect(alert.textContent).toContain("WireUnsupported");
    expect(alert.textContent).toContain("source text");
    log.push("fetch-panel: a WireUnsupported response renders ErrorState naming it");
  });
});

// ---- `PanelFrame`: one state at a time --------------------------------------------------------

/** A child that reads the panel context, so what `PanelFrame` provides is visible to a test. */
function ContextProbe() {
  const panel = usePanel();
  return (
    <dl>
      <dd data-testid="ctx-panel-id">{panel.panelId}</dd>
      <dd data-testid="ctx-gateway-url">{panel.response.url}</dd>
      <dd data-testid="ctx-status">{panel.response.status}</dd>
      <dd data-testid="ctx-bytes">{panel.response.bytes}</dd>
      <dd data-testid="ctx-text">{panel.response.text}</dd>
      <dd data-testid="ctx-fetched">{clockTime(panel.response.fetchedAt)}</dd>
    </dl>
  );
}

describe("PanelFrame renders exactly one state", () => {
  it("is aria-busy with three skeleton rows while loading", async () => {
    const body = recorded("eval.runs.listing");
    let release: (value: Response) => void = () => {};
    vi.stubGlobal(
      "fetch",
      vi.fn(() => new Promise<Response>((resolve) => {
        release = resolve;
      })),
    );

    const { container } = render(panelOver(body, ".runs[0].score"));

    const panel = container.querySelector("section");
    expect(panel?.getAttribute("aria-busy")).toBe("true");
    expect(container.querySelectorAll('[data-testid="skeleton-row"]')).toHaveLength(3);

    release(answer({ status: 200, text: body.text }));
    await waitFor(() => expect(container.querySelector("section")?.getAttribute("aria-busy")).toBe("false"));
    log.push("PanelFrame: loading is aria-busy with 3 skeleton rows");
  });

  it("renders its children over the parsed wire when the gateway answers 200", async () => {
    const body = recorded("eval.runs.listing");
    answersWith({ status: 200, text: body.text });
    render(panelOver(body, ".runs[1].score"));

    const figure = await screen.findByText("0.592181");
    expect(figure.getAttribute("data-path")).toBe(".runs[1].score");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("renders the empty state when its children find no rows", async () => {
    const body = recorded("empty.runs.listing");
    answersWith({ status: 200, text: body.text });
    render(
      <PanelFrame
        route={body.route}
        title="Runs"
        sourceLabel={SOURCE_LABEL}
        emptyText="no session has run yet"
        cliTwin='harnessed run "ping"'
      >
        {() => null}
      </PanelFrame>,
    );

    const said = await screen.findByText("No runs recorded yet.");
    expect(said.textContent).not.toContain(body.route);
    expect(screen.getByText('harnessed run "ping"')).toBeTruthy();
    expect(screen.getByText(/no session has run yet/)).toBeTruthy();
  });

  it("renders the absent state for a 404 and the error state for a 400", async () => {
    const missing = recorded("notfound.runs.run");
    answersWith({ status: 404, text: missing.text });
    const first = render(panelOver(missing, ".error"));
    expect((await screen.findByRole("alert")).textContent).toContain("404");
    expect(first.container.textContent).toContain(parseWire(missing.text, missing.route).at(".message").text);
    cleanup();

    const bad = recorded("badrequest.sessions.limit");
    answersWith({ status: 400, text: bad.text });
    render(panelOver(bad, ".error"));
    expect((await screen.findByRole("alert")).textContent).toContain("400");
  });

  it("shows only \"Updated N min ago\" in its footer, no route, status or bytes (D-9)", async () => {
    const body = recorded("health");
    answersWith({ status: 200, text: body.text });
    const { container } = render(panelOver(body, ".route_count"));

    await screen.findByText("37");
    const footer = container.querySelector("footer");
    expect(footer?.textContent).toContain("Updated 0 min ago");
    expect(footer?.textContent).not.toContain(body.route);
    expect(footer?.textContent).not.toContain("200");
    expect(footer?.textContent).not.toContain("bytes");
    expect(footer?.textContent).not.toContain("fetched");
    log.push("PanelFrame: the footer states only freshness, no endpoint string (D-9)");
  });

  it("holds the Updated badge and 0 buttons in its footer, always (ticket 211)", async () => {
    const body = recorded("health");
    answersWith({ status: 200, text: body.text });
    const { container } = render(panelOver(body, ".route_count"));
    await screen.findByText("37");
    expect(container.querySelector("footer")?.textContent).toContain("Updated 0 min ago");
    expect(container.querySelectorAll("footer button")).toHaveLength(0);
    expect(within(screen.getByRole("main")).queryAllByRole("button", { name: "Refresh" })).toHaveLength(0);
  });

  it("provides its RawResponse and a panelId to its children through a context", async () => {
    const body = recorded("health");
    answersWith({ status: 200, text: body.text });
    render(
      <PanelFrame route={body.route} title="Health" sourceLabel={SOURCE_LABEL}>
        {() => <ContextProbe />}
      </PanelFrame>,
    );

    await screen.findByTestId("ctx-text");
    expect(screen.getByTestId("ctx-text").textContent).toBe(body.text);
    expect(screen.getByTestId("ctx-status").textContent).toBe("200");
    expect(screen.getByTestId("ctx-bytes").textContent).toBe(
      String(new TextEncoder().encode(body.text).length),
    );
    expect(screen.getByTestId("ctx-gateway-url").textContent).toBe(`${GATEWAY_ORIGIN}/health`);
    expect(screen.getByTestId("ctx-fetched").textContent).toBe("14:03:09");
    expect(screen.getByTestId("ctx-panel-id").textContent).not.toBe("");
  });

  it("refuses to hand a figure a panel context it does not have", () => {
    expect(() => render(<ContextProbe />)).toThrow(/PanelFrame/);
  });

  it("fetches again when Refresh is pressed, and remembers nothing between the two (D-6)", async () => {
    const body = recorded("runs.listing");
    const spy = vi.fn(async () => answer({ status: 200, text: body.text }));
    vi.stubGlobal("fetch", spy);
    render(panelOver(body, ".runs[0].run_key"));

    await screen.findByText("eval_000000000HB774KP7M6WX8HT48:holdout-003-iso-date:1");
    expect(spy).toHaveBeenCalledTimes(1);

    const [refresh] = screen.getAllByRole("button", { name: "Refresh" });
    spy.mockImplementation(async () => answer({ status: 404, text: recorded("notfound.runs.run").text }));
    if (refresh) fireEvent.click(refresh);

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("404");
    // Nothing from the first answer survives the second (`D-6`): no last good value is shown.
    expect(screen.queryByText("eval_000000000HB774KP7M6WX8HT48:holdout-003-iso-date:1")).toBeNull();
    expect(spy).toHaveBeenCalledTimes(2);
    log.push("PanelFrame: Refresh re-requests and the previous answer is not kept (D-6)");
  });

  it("writes the state manifest this ticket declares as its evidence", () => {
    expect(log.length).toBeGreaterThan(6);
    mkdirSync(dirname(EVIDENCE), { recursive: true });
    writeFileSync(EVIDENCE, `${log.join("\n")}\n`, "utf8");
  });
});
