/**
 * `next.config.ts` under decision 44: a static export, with the one rewrite the development
 * loop needs and none in a production build (FR-63).
 *
 * The config is imported and its `rewrites()` called, rather than the file being read as text,
 * because the claim is about what Next would be handed: `[]` outside development, and the two
 * `/v1` and `/control/v1` forwards to the operator's gateway inside it. The text-level pins
 * (`output: "export"`, the guard's spelling, the single origin literal) are
 * `tests/test_dashboard_export.py`'s.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import config from "../../next.config";

afterEach(() => {
  vi.unstubAllEnvs();
});

async function rewritesUnder(env: string): Promise<unknown> {
  vi.stubEnv("NODE_ENV", env);
  const rewrites = config.rewrites;
  if (rewrites === undefined) throw new Error("next.config.ts declares no rewrites()");
  return rewrites();
}

describe("next.config.ts (decision 44)", () => {
  it("exports statically", () => {
    expect(config.output).toBe("export");
  });

  it("declares 0 rewrites in a production build, and 0 in test", async () => {
    expect(await rewritesUnder("production")).toEqual([]);
    expect(await rewritesUnder("test")).toEqual([]);
  });

  it("forwards /v1 and /control/v1 to the operator's gateway under next dev alone", async () => {
    expect(await rewritesUnder("development")).toEqual([
      { source: "/v1/:path*", destination: "http://127.0.0.1:8756/v1/:path*" },
      { source: "/control/v1/:path*", destination: "http://127.0.0.1:8756/control/v1/:path*" },
    ]);
  });
});
