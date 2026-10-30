/**
 * The dashboard's coverage scope and floor (decision 30, ticket 118).
 *
 * Two scopes, one number. `COVERAGE_SCOPE` unset - a narrow per-ticket verify - leaves
 * `coverage.include` off, so v8 scores the files the run actually imported. `COVERAGE_SCOPE=all`,
 * which the two verify-dashboard scripts export, puts the whole surface back in `include`, so a
 * file no test imports is still reported and still counts. The switch exists because in vitest 3
 * and later `include` reports a file whether or not a test loaded it (the old `coverage.all` flag
 * folded into it, and `--coverage.all=false` is inert on the pinned 5.0.0).
 *
 * The floor is read from `.github/coverage-ratchet-dashboard` here and nowhere else: one integer,
 * the same in both scopes, raised by a phase's last ticket and never lowered (MUST rule 5).
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { coverageConfigDefaults } from "vitest/config";

/** The one place the floor is read from. Never written. */
export const RATCHET = resolve(import.meta.dirname, "../.github/coverage-ratchet-dashboard");

/** Everything the full harness holds visible, imported or not. */
export const WHOLE_SURFACE = ["lib/**", "components/**", "app/**"];

/**
 * Loaded by a run but never part of the scored surface: the harness's own scaffolding. Without
 * `include` to bound it, v8 would otherwise report the setup file and the test supports.
 */
export const SCAFFOLDING = ["tests/**", "vitest.coverage.ts"];

export function coverageFloor(ratchet: string = RATCHET): number {
  const floor = Number.parseInt(readFileSync(ratchet, "utf8").trim(), 10);
  if (!Number.isInteger(floor)) throw new Error(`${ratchet} must hold one integer`);
  return floor;
}

export interface ScopeChoice {
  /** `"all"` selects the whole surface; anything else scores what the run imported. */
  scope?: string | undefined;
  /** What `"all"` means. The real config passes `WHOLE_SURFACE`; the floor fixture its own files. */
  wholeSurface?: string[];
  ratchet?: string;
}

/** The `test.coverage` block both scopes share. They differ in `include` and in nothing else. */
export function coverageOptions({
  scope = process.env.COVERAGE_SCOPE,
  wholeSurface = WHOLE_SURFACE,
  ratchet = RATCHET,
}: ScopeChoice = {}) {
  const floor = coverageFloor(ratchet);
  return {
    provider: "v8" as const,
    ...(scope === "all" ? { include: wholeSurface } : {}),
    exclude: [...coverageConfigDefaults.exclude, ...SCAFFOLDING],
    reporter: ["text" as const],
    thresholds: { lines: floor, branches: floor },
  };
}
