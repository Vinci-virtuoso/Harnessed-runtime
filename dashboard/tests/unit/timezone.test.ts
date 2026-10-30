/**
 * The suite does not depend on the machine's zone (FR-159, decision 71, ticket 230).
 *
 * `lib/humanize.ts` renders local wall time from UTC fixtures, so a case written on a UTC box can be
 * red on the operator's PC (ticket 227). The config pins `TZ` for the suite; this file proves the
 * humanize cases hold under two real zones, one ahead of UTC by an hour (`Africa/Lagos`) and one far
 * ahead (`Pacific/Auckland`), by running them in a child `vitest` with each zone set in its
 * environment, and comparing what each rendered.
 */
import { spawnSync } from "node:child_process";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const DASHBOARD = resolve(import.meta.dirname, "../..");
const VITEST = resolve(DASHBOARD, "node_modules/vitest/vitest.mjs");
// Two cold vitest starts in one case outrun the 5 s default; its own 60 s budget, the global stays.
const TIMEOUT_MS = 60_000;

/** A colour escape (`ESC [ ... m`): the verify harness forces colour on, so a piped child draws it. */
// eslint-disable-next-line no-control-regex
const ANSI = /\x1b\[[0-9;]*m/g;

type Run = { status: number | null; lines: string[]; stderr: string };

/**
 * The verbose reporter's one line per case, reduced to `mark title`. Vitest draws a pass as `√` on
 * Windows and `✓` elsewhere, and a piped child ends its lines `\r\n` there (ticket 263), so the
 * mark set takes both and the split takes either ending. The Windows harness exports `FORCE_COLOR`,
 * so the child drew each mark inside a colour escape and no line began with one: 0 lines read
 * (ticket 265). The escapes are stripped before the split, and the child is asked for none. The durations differ run to run and are
 * not part of what was rendered.
 */
export function renderedLines(stdout: string): string[] {
  return stdout
    .replace(ANSI, "")
    .split(/\r?\n/)
    .filter((line) => /^\s*[✓√×]/.test(line))
    .map((line) => line.trim().replace(/\s+\d+ms$/, ""));
}

/** One child run of the humanize unit file under `zone`, reduced to one `mark title` line per case. */
function runUnder(zone: string): Run {
  // The parent is itself a vitest worker; its markers would make the child believe it is nested.
  const env: NodeJS.ProcessEnv = { ...process.env };
  for (const key of Object.keys(env))
    if (key.startsWith("VITEST") || key === "FORCE_COLOR") delete env[key];
  const child = spawnSync(
    process.execPath,
    [
      VITEST,
      "run",
      "tests/unit/humanize.test.ts",
      "--coverage.enabled=false",
      "--reporter=verbose",
    ],
    {
      cwd: DASHBOARD,
      env: { ...env, TZ: zone, NO_COLOR: "1" },
      encoding: "utf8",
      timeout: 50_000,
    },
  );
  const lines = renderedLines(child.stdout);
  return { status: child.status, lines, stderr: child.stderr };
}

describe("reading a child run's lines", () => {
  const unix =
    "  ✓ tests/unit/humanize.test.ts > a case 12ms\n  × tests/unit/humanize.test.ts > b 3ms\nnoise\n";

  it("reads the same lines from a Windows run as from a POSIX one", () => {
    const windows = unix.replace(/✓/g, "√").replace(/\n/g, "\r\n");
    expect(renderedLines(unix)).toEqual([
      "✓ tests/unit/humanize.test.ts > a case",
      "× tests/unit/humanize.test.ts > b",
    ]);
    const coloured = unix
      .replace(/✓/g, "\x1b[32m✓\x1b[39m")
      .replace(/×/g, "\x1b[31m×\x1b[39m");
    expect(renderedLines(coloured)).toEqual(renderedLines(unix));
    expect(
      renderedLines(windows).map((line) => line.replace("√", "✓")),
    ).toEqual(renderedLines(unix));
  });
});

describe("the humanize cases under two zones", () => {
  it(
    "exit 0 under Africa/Lagos and under Pacific/Auckland with the same rendered lines",
    () => {
      const lagos = runUnder("Africa/Lagos");
      const auckland = runUnder("Pacific/Auckland");
      expect(lagos.status, lagos.stderr).toBe(0);
      expect(auckland.status, auckland.stderr).toBe(0);
      expect(lagos.lines.length).toBeGreaterThan(0);
      expect(auckland.lines).toEqual(lagos.lines);
    },
    TIMEOUT_MS,
  );
});
