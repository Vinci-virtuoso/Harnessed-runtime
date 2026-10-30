/**
 * The launch video's page captures (ticket 343, decision 110). Outside the e2e walk and never in
 * `scripts/verify-dashboard.*`: `npm run capture` is its own command. One `harnessed dashboard` per
 * home the manifest names, each on its own loopback port, serving read-only what that home holds.
 *
 * `demo` is a home seeded here through the CLI on the fake provider, as the README's four commands
 * leave it. `operator` and `memory-lab` are real homes on the operator's PC, given by
 * `HARNESSED_CAPTURE_OPERATOR_HOME` and `HARNESSED_CAPTURE_MEMORY_LAB_HOME` (each a `.harnessed`
 * directory). A home that is not given starts no server, and every capture over it fails by name.
 */
import { spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { defineConfig, devices } from "@playwright/test";

const REPO_ROOT = resolve(import.meta.dirname, "../../..");

/** Each home's port, and the variable that carries its served URL to the workers. */
export const HOMES = {
  demo: { port: 18770, given: "HARNESSED_CAPTURE_DEMO_HOME" },
  operator: { port: 18771, given: "HARNESSED_CAPTURE_OPERATOR_HOME" },
  "memory-lab": { port: 18772, given: "HARNESSED_CAPTURE_MEMORY_LAB_HOME" },
} as const;

function cli(home: string, argv: string[]): string {
  const done = spawnSync("uv", ["run", "harnessed", ...argv], {
    cwd: REPO_ROOT,
    env: { ...process.env, HARNESSED_HOME: home, HARNESSED_PROVIDER: "fake" },
    encoding: "utf8",
    shell: false,
  });
  if (done.status !== 0) {
    throw new Error(`harnessed ${argv.join(" ")} failed (${done.status}): ${done.stderr}${done.stdout}`);
  }
  return done.stdout;
}

/** The README's four commands over a fresh home, so Evals holds the run before and after. */
function seedDemo(): string {
  const home = join(mkdtempSync(join(tmpdir(), "harnessed-capture-")), "home");
  mkdirSync(home);
  cli(home, ["eval", "run", "smoke", "--fresh", "--provider", "fake"]);
  const dryRun = cli(home, ["improve", "run", "--dry-run", "--provider", "fake"]);
  const candidate = /^candidate (cand_[0-9a-f]+)$/m.exec(dryRun)?.[1];
  if (!candidate) throw new Error(`the dry run named no candidate:\n${dryRun}`);
  cli(home, ["improve", "promote", candidate, "--provider", "fake"]);
  cli(home, ["eval", "run", "smoke", "--fresh", "--provider", "fake"]);
  return home;
}

// Playwright loads this file in the runner and again in each worker; the runner seeds once and the
// workers inherit the variable, as the e2e config does with its home.
if (!process.env[HOMES.demo.given]) process.env[HOMES.demo.given] = seedDemo();

const servers = Object.entries(HOMES).flatMap(([name, { port, given }]) => {
  const home = process.env[given];
  if (!home) return [];
  const cwd = mkdtempSync(join(tmpdir(), `harnessed-capture-${name}-`));
  process.env[`HARNESSED_CAPTURE_URL_${name.toUpperCase().replace("-", "_")}`] = `http://127.0.0.1:${port}`;
  return [
    {
      command: `uv run harnessed dashboard --port ${port}`,
      cwd,
      url: `http://127.0.0.1:${port}/v1/health`,
      env: { HARNESSED_HOME: resolve(home), UV_PROJECT: REPO_ROOT, NEXT_TELEMETRY_DISABLED: "1" },
      reuseExistingServer: false,
      timeout: 120_000,
    },
  ];
});

export default defineConfig({
  testDir: ".",
  testMatch: "launch.spec.ts",
  outputDir: "./test-results",
  fullyParallel: false,
  workers: 1,
  forbidOnly: true,
  retries: 0,
  reporter: [["list"]],
  use: { viewport: { width: 1920, height: 1080 }, trace: "off" },
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 1920, height: 1080 },
        // A machine without Playwright's own browser (a sandbox whose download is blocked) may name
        // an installed Chromium instead; unset, Playwright uses the one `npx playwright install` put down.
        launchOptions: { executablePath: process.env.HARNESSED_CAPTURE_CHROMIUM || undefined },
      },
    },
  ],
  webServer: servers,
});
