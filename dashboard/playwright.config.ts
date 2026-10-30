/**
 * End to end against one live loopback process (decision 30, FR-66). `webServer` starts a seeded
 * `harnessed dashboard` on 18756: the UI at `/` and the read gateway mounted at `/v1`, in one
 * process. The operator's 8756 is never used, and the unreachable case is a `page.route` abort
 * of `/v1/**` in the spec rather than a second server.
 */
import { mkdirSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { defineConfig, devices } from "@playwright/test";

import {
  DASHBOARD_PORT,
  FAKE_LATENCY_MS,
  FAKE_LATENCY_VARIABLE,
  GATEWAY_URL,
  HOME_VARIABLE,
  REPO_ROOT,
  STUB_BASE_URL,
  serverDirectory,
} from "./tests/e2e/harness";

// `dashboard` refuses a home that does not exist, and it starts before globalSetup seeds, so the
// directory is made here, once, in the runner; workers inherit the variable and reuse it.
if (!process.env[HOME_VARIABLE]) {
  const home = join(mkdtempSync(join(tmpdir(), "harnessed-e2e-")), "home");
  mkdirSync(home);
  process.env[HOME_VARIABLE] = home;
}
const home = process.env[HOME_VARIABLE];

// The server runs from a directory of its own beside the home, never the checkout: the models walk
// saves a key, and the control app writes `.env` into its working directory (ticket 149). `uv` is
// pointed back at the checkout with `UV_PROJECT`, so the one command stays the one command.
const cwd = serverDirectory(home);
mkdirSync(cwd, { recursive: true });

// Slow the fake provider down for this run and nothing else (ticket 119). Set on the runner
// rather than on the `webServer` entry: the spec that starts the CLI spawns it with
// `{ ...process.env }`, so the run inherits the delay from here, and every worker agrees on it.
process.env[FAKE_LATENCY_VARIABLE] = String(FAKE_LATENCY_MS);

export default defineConfig({
  testDir: "./tests/e2e",
  outputDir: "./test-results",
  fullyParallel: false,
  workers: 1,
  forbidOnly: true,
  retries: 0,
  reporter: [["list"]],
  globalSetup: "./tests/e2e/global-setup.ts",
  globalTeardown: "./tests/e2e/global-teardown.ts",
  use: {
    viewport: { width: 1280, height: 800 },
    trace: "off",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: `uv run harnessed dashboard --port ${DASHBOARD_PORT}`,
    cwd,
    url: `${GATEWAY_URL}/health`,
    env: {
      HARNESSED_HOME: home,
      HARNESSED_PROVIDER: "fake",
      UV_PROJECT: REPO_ROOT,
      NEXT_TELEMETRY_DISABLED: "1",
      // Only a keyed provider reads it, and only the models walk asks one (ticket 149).
      HARNESSED_BASE_URL: STUB_BASE_URL,
      [FAKE_LATENCY_VARIABLE]: String(FAKE_LATENCY_MS),
    },
    reuseExistingServer: false,
    timeout: 120_000,
  },
});
