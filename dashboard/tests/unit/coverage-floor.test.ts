/**
 * Ticket 118 - the floor can still fail, in both scopes.
 *
 * Decision 30 now says the dashboard is scored two ways off one number: a narrow per-ticket verify
 * over the files its run imported, the full harness over `lib/**`, `components/**` and `app/**`
 * whether a test loaded them or not. MUST rule 12 is why this file exists: an assertion that the
 * config *says* `thresholds: 80` cannot detect a floor that no longer fires. So the four cases
 * below run real `vitest --coverage` processes over `tests/floor`, a fixture whose two files have a
 * known number, and read their exit codes.
 *
 * The fixture's config calls the same `coverageOptions` the dashboard's own config calls, and hands
 * it its own two files as the whole surface. The mechanism under test is therefore the shipped one;
 * only the surface it is pointed at is small enough to run four times.
 */
import { spawnSync } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { describe, expect, it, vi } from "vitest";
import { RATCHET, WHOLE_SURFACE, coverageFloor, coverageOptions } from "../../vitest.coverage";

const DASHBOARD = resolve(import.meta.dirname, "../..");
const FIXTURE = resolve(DASHBOARD, "tests/floor");
const VITEST = resolve(DASHBOARD, "node_modules/vitest/vitest.mjs");
const EVIDENCE = resolve(DASHBOARD, "../evidence/118/floor-fires.txt");

/** Two real vitest processes per case, each booting vite and v8 under a suite already at load. */
const FIXTURE_RUNS_MS = 180_000;

const log: string[] = [];

interface Run {
  status: number;
  output: string;
}

/** Runs `body` with `COVERAGE_SCOPE` set to exactly this scope, and puts the environment back. */
function withScope<T>(scope: string | undefined, body: () => T): T {
  const before = process.env.COVERAGE_SCOPE;
  if (scope === undefined) delete process.env.COVERAGE_SCOPE;
  else process.env.COVERAGE_SCOPE = scope;
  try {
    return body();
  } finally {
    if (before === undefined) delete process.env.COVERAGE_SCOPE;
    else process.env.COVERAGE_SCOPE = before;
  }
}

/** The same, for a body that has to be awaited inside the scope rather than after it. */
async function withScopeAsync<T>(scope: string | undefined, body: () => Promise<T>): Promise<T> {
  const before = process.env.COVERAGE_SCOPE;
  if (scope === undefined) delete process.env.COVERAGE_SCOPE;
  else process.env.COVERAGE_SCOPE = scope;
  try {
    return await body();
  } finally {
    if (before === undefined) delete process.env.COVERAGE_SCOPE;
    else process.env.COVERAGE_SCOPE = before;
  }
}

/** One real coverage run over the fixture, in the named scope. */
function runFixture(scope: "narrow" | "all", tests: string[]): Run {
  const env = { ...process.env };
  // The full harness exports COVERAGE_SCOPE=all, so the narrow case must unset it, not merely
  // leave it alone: inheriting the parent's scope would make this test pass for the wrong reason.
  delete env.COVERAGE_SCOPE;
  if (scope === "all") env.COVERAGE_SCOPE = "all";

  const run = spawnSync(process.execPath, [VITEST, "run", "--coverage", ...tests], {
    cwd: FIXTURE,
    env,
    encoding: "utf8",
    timeout: 180_000,
  });
  const output = `${run.stdout ?? ""}${run.stderr ?? ""}`;
  log.push(
    `${scope} scope, ${tests.join(" ")} -> exit ${run.status}`,
    ...output
      .split("\n")
      .filter((line) => line.includes("% ( ") || line.includes("ERROR: Coverage"))
      .map((line) => `    ${line.trim()}`),
  );
  return { status: run.status ?? -1, output };
}

describe("the floor is one integer, read from the ratchet and written nowhere else", () => {
  it("reads 80 from .github/coverage-ratchet-dashboard and applies it to lines and branches", () => {
    expect(RATCHET.endsWith("coverage-ratchet-dashboard")).toBe(true);
    expect(coverageFloor()).toBe(80);
    for (const scope of [undefined, "all"]) {
      expect(withScope(scope, () => coverageOptions()).thresholds).toStrictEqual({
        lines: 80,
        branches: 80,
      });
    }
    log.push(`floor: ${coverageFloor()} for lines and branches, read from ${RATCHET}`);
  });

  it("differs between the two scopes in the include list and in nothing else", () => {
    const narrow: Record<string, unknown> = withScope(undefined, () => ({ ...coverageOptions() }));
    const all: Record<string, unknown> = withScope("all", () => ({ ...coverageOptions() }));
    expect(all.include).toStrictEqual(WHOLE_SURFACE);
    expect("include" in narrow).toBe(false);
    delete all.include;
    expect(narrow).toStrictEqual(all);
    log.push(`scopes: narrow has no include; all has ${WHOLE_SURFACE.join(", ")}`);
  });

  it("is the block the dashboard's own config ships, in both scopes", async () => {
    for (const scope of [undefined, "all"] as const) {
      const expected = withScope(scope, () => coverageOptions());
      vi.resetModules();
      const loaded = await withScopeAsync(scope, () => import("../../vitest.config"));
      expect(loaded.default.test?.coverage).toStrictEqual(expected);
    }
  });
});

describe("the floor fires under it and passes over it, in both scopes", () => {
  it("narrow: fails a run under the floor and passes one over it", () => {
    const under = runFixture("narrow", ["low.test.ts"]);
    expect(under.output).toContain("ERROR: Coverage for lines");
    expect(under.status, "a run under the floor exited 0").not.toBe(0);

    const over = runFixture("narrow", ["high.test.ts"]);
    expect(over.output).not.toContain("ERROR: Coverage");
    expect(over.status, over.output).toBe(0);
  }, FIXTURE_RUNS_MS);

  it("all: scores the file no test imported, so the same run that passed narrow now fails", () => {
    // `high.test.ts` covers every branch it imports and passed above. In the whole-surface scope
    // `unvisited.ts` is reported at 0% beside it, which is the guarantee the full harness keeps.
    const under = runFixture("all", ["high.test.ts"]);
    expect(under.output).toContain("unvisited.ts");
    expect(under.output).toContain("ERROR: Coverage for lines");
    expect(under.status, "the unimported file did not count against the floor").not.toBe(0);

    const over = runFixture("all", ["high.test.ts", "whole.test.ts"]);
    expect(over.output).not.toContain("ERROR: Coverage");
    expect(over.status, over.output).toBe(0);
  }, FIXTURE_RUNS_MS);

  it("writes the manifest this ticket declares", () => {
    expect(log.length).toBeGreaterThan(6);
    mkdirSync(dirname(EVIDENCE), { recursive: true });
    writeFileSync(EVIDENCE, `${log.join("\n")}\n`, "utf8");
  });
});
