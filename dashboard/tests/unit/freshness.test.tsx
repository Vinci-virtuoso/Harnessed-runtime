// @vitest-environment jsdom
/**
 * FR-52: a panel says how old it is and remembers nothing (`D-5`, `D-6`; decision 34).
 *
 * Three layers, each able to fail on its own:
 *   - `lib/clock.ts` owns the five minute mark and the sentence `design.md` documents, and is one
 *     of the two files the decision 32 lint allowlists because it does arithmetic on the
 *     dashboard's own clock and never on a gateway value;
 *   - `StaleBadge` is the warn-coloured footer chip that appears at that mark and nowhere else;
 *   - `PanelFrame` ages its one answer on that clock, announces the crossing once through its own
 *     live region, and holds nothing - not a previous answer, not a byte of browser storage.
 *
 * The clock is fake throughout, so "4 min 59 s" and "5 min" are exact rather than approximate, and
 * so a test can let half an hour pass and count the requests that were made in it: `D-6` says a
 * data route is read on navigation and on `Refetch`, and at no other time.
 *
 * The inputs are the recorded gateway responses (decision 33), swept whole rather than sampled, so
 * a re-record widens this test for free.
 */
import { mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";

import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { WithRefresh } from "../support/with-refresh";
import { PanelFrame } from "../../components/PanelFrame";
import { StaleBadge } from "../../components/StaleBadge";
import { UnreachableState } from "../../components/UnreachableState";
import { Value } from "../../components/Value";
import { STALE_AFTER_MINUTES, clockTime, isStale, minutesAgo, minutesSince } from "../../lib/clock";
import { GATEWAY_ORIGIN, fetchPanel } from "../../lib/fetch-panel";
import { nullReasonFor } from "../../lib/null-reasons";
import { parseWire, type Wire } from "../../lib/wire";
import { SLOW_SWEEP_MS } from "../support/slow-sweep";

const SOURCE_LABEL = "Ledger: all time";

const DASHBOARD = resolve(import.meta.dirname, "../..");
const GATEWAY_FIXTURES = resolve(DASHBOARD, "tests/fixtures/gateway");
const EVIDENCE = resolve(DASHBOARD, "../evidence/103/freshness.txt");

/** The dashboard's clock, frozen so "fetched hh:mm:ss" is the same characters on every run. */
const FROZEN = new Date("2026-09-17T14:03:09");

/** Milliseconds, spelled once so the durations below read as the times they are. */
const SECOND = 1000;
const MINUTE = 60 * SECOND;

/** The lines this run writes to `evidence/103/freshness.txt`. */
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

/**
 * Everything an open drawer will show of the kept response, exercised on one body: the record it
 * was read from, and both edges of the focus trap. None of it is a second read, and none of it is
 * written anywhere.
 */
function drawerHoldsTheResponse(dialog: HTMLElement): void {
  expect(within(dialog).getByTestId("record-view")).toBeTruthy();

  const controls = within(dialog).getAllByRole("button");
  const first = controls.at(0);
  const last = controls.at(-1);
  last?.focus();
  fireEvent.keyDown(dialog, { key: "Tab" });
  expect(document.activeElement).toBe(first);
  fireEvent.keyDown(dialog, { key: "Tab", shiftKey: true });
  expect(document.activeElement).toBe(last);
  // A key that is not Tab, and a Tab away from either edge, are both left entirely alone.
  fireEvent.keyDown(dialog, { key: "ArrowDown" });
  first?.focus();
  fireEvent.keyDown(dialog, { key: "Tab" });
  expect(document.activeElement).toBe(first);
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

/** Stub `fetch` with one answer, and hand back the spy so a test can count the reads it made. */
function answersWith(next: Answer): ReturnType<typeof vi.fn> {
  const spy = vi.fn(async () => answer(next));
  vi.stubGlobal("fetch", spy);
  return spy;
}

/** A panel over one recorded response, rendering its first figure so `ok` has something to show. */
function panelOver(body: Recorded, path?: string) {
  const cited = path ?? firstLeafPath(body.text);
  const children = (wire: Wire) =>
    cited === undefined ? null : (
      <Value leaf={wire.at(cited)} nullReason={nullReasonFor(body.route, cited)} />
    );
  return (
    <WithRefresh>
      <PanelFrame route={body.route} title="Panel" sourceLabel={SOURCE_LABEL}>
        {children}
      </PanelFrame>
    </WithRefresh>
  );
}

/**
 * Let the dashboard's clock run. Every state change here is a render or a timer - never a request -
 * so the fetch spy's call count after a `pass` is the claim `D-6` makes.
 */
async function pass(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

/** Everything the dashboard must never write to, watched for the length of a test. */
type StorageWatch = {
  written: string[];
  indexedDbOpens: ReturnType<typeof vi.fn>;
  serviceWorkerRegistrations: ReturnType<typeof vi.fn>;
};

let storage: StorageWatch;

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date", "setTimeout", "clearTimeout", "setInterval", "clearInterval"] });
  vi.setSystemTime(FROZEN);

  const written: string[] = [];
  vi.spyOn(Storage.prototype, "setItem").mockImplementation((key: string) => {
    written.push(key);
  });
  const indexedDbOpens = vi.fn();
  const serviceWorkerRegistrations = vi.fn();
  vi.stubGlobal("indexedDB", { open: indexedDbOpens, deleteDatabase: vi.fn() });
  Object.defineProperty(navigator, "serviceWorker", {
    configurable: true,
    value: { register: serviceWorkerRegistrations, getRegistrations: vi.fn(async () => []) },
  });
  storage = { written, indexedDbOpens, serviceWorkerRegistrations };
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

/** Nothing was remembered: no storage key written, no database opened, no worker registered. */
function rememberedNothing(): void {
  expect(storage.written).toEqual([]);
  expect(storage.indexedDbOpens).not.toHaveBeenCalled();
  expect(storage.serviceWorkerRegistrations).not.toHaveBeenCalled();
  expect(localStorage.length).toBe(0);
  expect(sessionStorage.length).toBe(0);
}

// ---- `lib/clock.ts`: the five minute mark ------------------------------------------------------

describe("the five minute mark (D-5)", () => {
  it("is 5 minutes on the dashboard's clock, and not a minute earlier", () => {
    expect(STALE_AFTER_MINUTES).toBe(5);
    const at = (ms: number) => new Date(FROZEN.getTime() + ms);

    expect(isStale(FROZEN, at(0))).toBe(false);
    expect(isStale(FROZEN, at(4 * MINUTE + 59 * SECOND))).toBe(false);
    expect(isStale(FROZEN, at(5 * MINUTE - 1))).toBe(false);
    expect(isStale(FROZEN, at(5 * MINUTE))).toBe(true);
    expect(isStale(FROZEN, at(7 * MINUTE))).toBe(true);
    log.push("clock: 4m59s is fresh, 5m00s is stale, on the dashboard's own clock");
  });

  it("counts whole elapsed minutes and never rounds one up", () => {
    const at = (ms: number) => new Date(FROZEN.getTime() + ms);

    expect(minutesSince(FROZEN, at(0))).toBe("0");
    expect(minutesSince(FROZEN, at(59 * SECOND))).toBe("0");
    expect(minutesSince(FROZEN, at(4 * MINUTE + 59 * SECOND))).toBe("4");
    expect(minutesSince(FROZEN, at(5 * MINUTE))).toBe("5");
    expect(minutesSince(FROZEN, at(7 * MINUTE + 30 * SECOND))).toBe("7");
  });

  it('says "N min ago", the words PanelFrame\'s footer and Value\'s tooltip both build on', () => {
    const at = (ms: number) => new Date(FROZEN.getTime() + ms);

    expect(minutesAgo(FROZEN, at(5 * MINUTE))).toBe("5 min ago");
    expect(minutesAgo(FROZEN, at(7 * MINUTE))).toBe("7 min ago");
    expect(clockTime(FROZEN)).toBe("14:03:09");
    log.push('clock: minutesAgo says "5 min ago" at the five minute mark');
  });
});

// ---- `StaleBadge` -----------------------------------------------------------------------------

describe("StaleBadge", () => {
  it("renders the label it is given and nothing it worked out for itself, warn only when told", () => {
    const { rerender } = render(<StaleBadge label="7 min ago" warn={false} />);
    expect(screen.getByTestId("stale-badge").textContent).toBe("7 min ago");
    expect(screen.getByTestId("stale-badge").getAttribute("data-warn")).toBe("false");

    rerender(<StaleBadge label="7 min ago" warn={true} />);
    expect(screen.getByTestId("stale-badge").getAttribute("data-warn")).toBe("true");
  });
});

// ---- `PanelFrame`: the stale state -------------------------------------------------------------

describe("a panel dates itself and its footer turns color-warn at five minutes (D-5, D-9)", () => {
  const body = recorded("runs.listing");

  it('says "Updated N min ago" from minute 0, warn only at 5 min and after', async () => {
    answersWith({ status: 200, text: body.text });
    const { container } = render(panelOver(body));
    await pass(0);

    const footer = container.querySelector("footer");
    expect(footer?.textContent).toContain("Updated 0 min ago");
    expect(footer?.textContent).not.toContain("fetched");
    expect(footer?.textContent).not.toContain("bytes");
    expect(footer?.textContent).not.toContain(body.route);
    const badge = screen.getByTestId("stale-badge");
    expect(badge.getAttribute("data-warn")).toBe("false");

    await pass(4 * MINUTE + 59 * SECOND);
    expect(screen.getByTestId("stale-badge").textContent).toBe("Updated 4 min ago");
    expect(screen.getByTestId("stale-badge").getAttribute("data-warn")).toBe("false");

    await pass(SECOND);
    expect(screen.getByTestId("stale-badge").textContent).toBe("Updated 5 min ago");
    expect(screen.getByTestId("stale-badge").getAttribute("data-warn")).toBe("true");
    log.push('panel: "Updated N min ago" from minute 0, data-warn true only at 5 min');
  });

  it("says only 'Updated N min ago' in the footer, nothing else (D-9)", async () => {
    answersWith({ status: 200, text: body.text });
    const { container } = render(panelOver(body));
    await pass(0);

    const footer = container.querySelector("footer") as HTMLElement;
    expect(within(footer).queryAllByRole("button")).toHaveLength(0);
    expect(footer.querySelectorAll("*").length).toBeLessThanOrEqual(2);
    log.push("panel: the footer holds only the freshness words (D-9, ticket 211)");
  });

  it("announces the crossing once through the panel's live region, then keeps counting", async () => {
    answersWith({ status: 200, text: body.text });
    const { container } = render(panelOver(body));
    await pass(0);

    const live = container.querySelector('[data-testid="panel-announcement"]');
    expect(live?.getAttribute("aria-live")).toBe("polite");
    expect(live?.textContent).toBe("");

    await pass(5 * MINUTE);
    expect(live?.textContent).toBe("Updated 5 min ago");

    // Two more minutes: the badge keeps counting, the announcement is not made a second time.
    await pass(2 * MINUTE);
    expect(screen.getByTestId("stale-badge").textContent).toBe("Updated 7 min ago");
    expect(live?.textContent).toBe("Updated 5 min ago");
    log.push("panel: the stale crossing is announced once, while the badge keeps counting");
  });

  it("re-dates the footer when Refresh succeeds", async () => {
    const spy = answersWith({ status: 200, text: body.text });
    const { container } = render(panelOver(body));
    await pass(0);
    await pass(5 * MINUTE);
    expect(screen.getByTestId("stale-badge").getAttribute("data-warn")).toBe("true");

    const [refresh] = screen.getAllByRole("button", { name: "Refresh" });
    if (refresh) fireEvent.click(refresh);
    await pass(0);

    expect(screen.getByTestId("stale-badge").textContent).toBe("Updated 0 min ago");
    expect(screen.getByTestId("stale-badge").getAttribute("data-warn")).toBe("false");
    expect(container.querySelector('[data-testid="panel-announcement"]')?.textContent).toBe("");
    expect(spy).toHaveBeenCalledTimes(2);
    log.push("panel: Refresh clears the warn state and the announcement, and re-dates the footer");
  });
});

// ---- `D-6`: no data route polls ---------------------------------------------------------------

describe("a mounted panel reads its route exactly once (D-6, decision 34)", () => {
  it("makes 1 request in half an hour with no navigation, no Refetch and no stream", async () => {
    const body = recorded("eval.runs.listing");
    const spy = answersWith({ status: 200, text: body.text });
    render(panelOver(body));
    await pass(0);
    expect(spy).toHaveBeenCalledTimes(1);

    await pass(30 * MINUTE);

    expect(spy).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("stale-badge").textContent).toBe("Updated 30 min ago");
    rememberedNothing();
    log.push("D-6: 1 request in 30 minutes; the panel ages rather than refetching");
  });

  it("reads again on navigation - a new route is a new read, not a second one of the same", async () => {
    const first = recorded("runs.listing");
    const second = recorded("sessions.listing");
    const spy = vi.fn(async (url: string) =>
      answer({ status: 200, text: url.endsWith(second.route) ? second.text : first.text }),
    );
    vi.stubGlobal("fetch", spy);

    const { rerender } = render(panelOver(first));
    await pass(0);
    expect(spy).toHaveBeenCalledTimes(1);

    rerender(panelOver(second));
    await pass(0);

    expect(spy).toHaveBeenCalledTimes(2);
    expect(spy.mock.calls.map(([url]) => url)).toEqual([first.route, second.route]);
  });
});

// ---- `D-6`: a failure keeps nothing of the answer before it -----------------------------------

describe("a failed read leaves 0 figures of the one before it (D-6)", () => {
  const body = recorded("runs.listing");
  /* A run key, so "0 figures of the earlier response remain" is asked of characters no error body
     could spell for itself: the first scalar of this body is `1`, which a byte count also holds. */
  const KEY = ".runs[0].run_key";

  it("shows the unreachable state and 0 earlier figures when the gateway goes away", async () => {
    const spy = answersWith({ status: 200, text: body.text });
    const { container } = render(panelOver(body, KEY));
    await pass(0);

    const kept = container.querySelector("[data-figure]")?.textContent ?? "";
    expect(kept).toBe("eval_000000000HB774KP7M6WX8HT48:holdout-003-iso-date:1");

    spy.mockImplementation(async () => {
      throw new TypeError("Failed to fetch");
    });
    const [refresh] = screen.getAllByRole("button", { name: "Refresh" });
    if (refresh) fireEvent.click(refresh);
    await pass(0);

    expect(screen.getByRole("heading", { name: "Gateway unreachable" })).toBeTruthy();
    expect(container.querySelectorAll("[data-figure]")).toHaveLength(0);
    expect(container.textContent).not.toContain(kept);
    expect(container.querySelector("footer")).toBeNull();
    expect(screen.queryByTestId("stale-badge")).toBeNull();
    rememberedNothing();
    log.push("D-6: an unreachable refetch leaves 0 figures of the answer before it");
  });

  it("shows the gateway's 500 and 0 earlier figures when the database goes away", async () => {
    const broken = recorded("nodatabase.runs.listing");
    const spy = answersWith({ status: 200, text: body.text });
    const { container } = render(panelOver(body, KEY));
    await pass(0);
    const kept = container.querySelector("[data-figure]")?.textContent ?? "";
    expect(kept).not.toBe("");

    spy.mockImplementation(async () => answer({ status: 500, text: broken.text }));
    const [refresh] = screen.getAllByRole("button", { name: "Refresh" });
    if (refresh) fireEvent.click(refresh);
    await pass(0);

    const alert = screen.getByRole("alert");
    expect(alert.textContent).toContain("500");
    expect(alert.textContent).toContain("StorageError");
    expect(container.querySelectorAll("[data-figure]")).toHaveLength(0);
    expect(container.textContent).not.toContain(kept);
    log.push("D-6: a 500 refetch leaves 0 figures of the answer before it");
  });

  it("goes stale again on the failure's own clock, not on the one before it", async () => {
    const bad = recorded("badrequest.runs.limit");
    const spy = answersWith({ status: 200, text: body.text });
    render(panelOver(body));
    await pass(4 * MINUTE);

    spy.mockImplementation(async () => answer({ status: 400, text: bad.text }));
    const [refresh] = screen.getAllByRole("button", { name: "Refresh" });
    if (refresh) fireEvent.click(refresh);
    await pass(0);

    // Four minutes had passed under the first answer; the second answer starts at zero.
    await pass(4 * MINUTE);
    expect(screen.getByTestId("stale-badge").getAttribute("data-warn")).toBe("false");
    await pass(MINUTE);
    expect(screen.getByTestId("stale-badge").textContent).toBe("Updated 5 min ago");
    expect(screen.getByTestId("stale-badge").getAttribute("data-warn")).toBe("true");
  });
});

// ---- `D-6`: nothing is written down -----------------------------------------------------------

describe("a full render writes nothing to browser storage (D-6)", () => {
  it("writes 0 keys, opens 0 databases and registers 0 service workers, over every state", async () => {
    const states: Recorded[] = [
      recorded("health"),
      recorded("empty.runs.listing"),
      recorded("notfound.runs.run"),
      recorded("badrequest.runs.limit"),
      recorded("transport-unreachable"),
    ];

    for (const state of states) {
      answersWith({ status: state.code, text: state.text });
      render(panelOver(state));
      await pass(0);
      await pass(6 * MINUTE);
      cleanup();
    }

    rememberedNothing();
    log.push(`D-6: ${states.length} panel states rendered, 0 bytes written to browser storage`);
  });
});

// ---- What a panel dates, and what it cannot ----------------------------------------------------

describe("what a panel dates, and what it cannot", () => {

  it("has no time to state where the gateway was never reached, and no cause to quote either", () => {
    const { container } = render(<UnreachableState />);

    expect(screen.getByRole("heading", { name: "Gateway unreachable" })).toBeTruthy();
    expect(container.querySelector('[data-testid="unreachable-cause"]')).toBeNull();
    expect(container.textContent).not.toContain("dashboard clock");
    log.push("panel: an unreachable read has no fetch time to state and none is invented");
  });

  it("reports a thrown non-Error as itself rather than as a guess at one", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw "the browser refused";
      }),
    );

    const result = await fetchPanel("/v1/runs", GATEWAY_ORIGIN);

    expect(result.state).toBe("unreachable");
    if (result.state !== "unreachable") return;
    expect(result.cause).toBe("the browser refused");
  });

  it("names an Error with no message by its class, so an empty cause is never shown", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("");
      }),
    );

    const result = await fetchPanel("/v1/runs", GATEWAY_ORIGIN);

    expect(result.state).toBe("unreachable");
    if (result.state !== "unreachable") return;
    expect(result.cause).toBe("TypeError");
  });

  it("a figure outside a panel has no panel clock and no source to open", () => {
    const wire = parseWire('{"x":null}', "/v1/health");
    const { container } = render(<Value leaf={wire.at(".x")} />);

    expect(container.querySelector("footer")).toBeNull();
    expect(screen.queryByRole("button")).toBeNull();
    expect(screen.getByText("unknown").getAttribute("aria-label")).toBe("unknown");
  });

  it("dates an empty panel like any other, beside the command that would fill it", async () => {
    const empty = recorded("empty.runs.listing");
    answersWith({ status: 200, text: empty.text });
    const { container } = render(
      <PanelFrame
        route={empty.route}
        title="Runs"
        sourceLabel={SOURCE_LABEL}
        emptyText="no session has run yet"
        cliTwin='harnessed run "ping"'
      >
        {() => null}
      </PanelFrame>,
    );
    await pass(0);

    expect(screen.getByText('harnessed run "ping"')).toBeTruthy();
    expect(container.querySelector("footer")?.textContent).toContain("Updated 0 min ago");

    await pass(5 * MINUTE);
    expect(screen.getByTestId("stale-badge").textContent).toBe("Updated 5 min ago");
    log.push("panel: a 200 with zero rows is dated and goes stale like any other answer");
  });

  it("opens a figure's source on Enter as well as on a click, and dates neither read again", async () => {
    const body = recorded("health");
    const spy = answersWith({ status: 200, text: body.text });
    const { container } = render(panelOver(body));
    await pass(0);

    const trigger = container.querySelector("[data-figure]") as HTMLElement;
    fireEvent.keyDown(trigger, { key: "Enter" });
    expect(screen.getByRole("dialog").textContent).toContain("Where this came from");
    fireEvent.keyDown(document.body, { key: "Escape" });
    await pass(0);

    expect(spy).toHaveBeenCalledTimes(1);
    rememberedNothing();
  });
});

// ---- The recorded corpus, swept whole ----------------------------------------------------------

describe("every recorded response ages on the same clock and is remembered by nothing", () => {
  it("goes stale at 5 minutes, is read once, and keeps its raw body only in the open drawer", async () => {
    const bodies = everyRecordedBody();
    const walked: string[] = [];

    for (const body of bodies) {
      const spy = answersWith({
        status: body.code,
        text: body.text,
      });
      const { container, unmount } = render(panelOver(body));
      await pass(0);
      expect(spy, `${body.name} was read more than once`).toHaveBeenCalledTimes(1);

      // Where a figure rendered, it is its own trigger and opens the response this panel fetched,
      // and the drawer holds that record for exactly as long as it is open: reading it, walking
      // its controls, all read the one kept response, and closing it leaves nothing behind for the
      // next open to find.
      const trigger = container.querySelector("[data-figure]");
      if (trigger) {
        fireEvent.click(trigger);
        const dialog = screen.getByRole("dialog");
        expect(dialog.textContent).toContain("Where this came from");
        drawerHoldsTheResponse(dialog);
        fireEvent.keyDown(dialog, { key: "Escape" });
        await pass(0);
        expect(screen.queryByRole("dialog")).toBeNull();
      }

      await pass(5 * MINUTE);
      const footer = container.querySelector("footer");
      const badge = screen.queryByTestId("stale-badge");
      if (footer === null) {
        // Unreachable is the one state with no response to date, so it has nothing to call stale.
        expect(badge, `${body.name} showed a badge with no footer`).toBeNull();
      } else {
        expect(badge?.textContent, `${body.name} did not go stale`).toBe("Updated 5 min ago");
      }

      expect(spy, `${body.name} was read again while it sat`).toHaveBeenCalledTimes(1);
      unmount();
      walked.push(body.name);
    }

    expect(walked.length).toBeGreaterThan(40);
    rememberedNothing();
    log.push(`corpus: ${walked.length} recorded responses, each read once and each stale at 5m`);
  // The whole recorded corpus in one test: about 50 renders, each with a drawer open and close.
  // It passes in a second or two alone and takes far longer when the suite fills every worker, so
  // the ceiling is contention, not the sweep (ticket 118). Widen the ceiling rather than thinning
  // what is swept.
  }, SLOW_SWEEP_MS);
});

// ---- Evidence -----------------------------------------------------------------------------------

describe("evidence", () => {
  it("writes the manifest this ticket declares", () => {
    expect(log.length).toBeGreaterThan(6);
    mkdirSync(dirname(EVIDENCE), { recursive: true });
    writeFileSync(EVIDENCE, `${log.join("\n")}\n`, "utf8");
  });
});
