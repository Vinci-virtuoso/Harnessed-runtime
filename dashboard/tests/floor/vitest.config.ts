/**
 * The floor fixture's own root (ticket 118). It runs the real `coverageOptions` switch over two
 * tiny files, so `tests/unit/coverage-floor.test.ts` can watch the floor fail under it and pass
 * over it in both scopes without running the dashboard's own suite four times.
 */
import { defineConfig } from "vitest/config";
import { coverageOptions } from "../../vitest.coverage";

export default defineConfig({
  test: {
    environment: "node",
    include: ["*.test.ts"],
    coverage: coverageOptions({ wholeSurface: ["subject.ts", "unvisited.ts"] }),
  },
});
