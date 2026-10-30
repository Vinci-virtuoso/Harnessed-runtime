/**
 * Unit tests (decision 30). The coverage scope and floor live in `vitest.coverage.ts`: one number,
 * read from `.spec/coverage-ratchet-dashboard` and never written here, and two scopes off it - the
 * files a narrow run imported, or the whole surface when the verify scripts export
 * `COVERAGE_SCOPE=all` (ticket 118). The socket guard is a setup file, so no test can load before
 * it.
 */
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";
import { coverageOptions } from "./vitest.coverage";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "node",
    // The served timestamps are UTC and `lib/humanize.ts` renders local wall time, so the suite pins
    // the zone rather than the product (ticket 227).
    env: { TZ: "UTC" },
    maxWorkers: 4,
    include: ["tests/unit/**/*.test.{ts,tsx}"],
    setupFiles: ["tests/setup/socket-guard.ts"],
    coverage: coverageOptions(),
  },
});
