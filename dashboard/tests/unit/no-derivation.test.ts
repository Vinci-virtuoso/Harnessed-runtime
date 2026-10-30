/**
 * The decision 32 lint rule fires (FR-49). A check that the rule is configured cannot see a rule
 * that never fires (MUST rule 12), so one test lints the real tree (0 findings) and the seeded
 * fixture (both derivations reported): neither branch can rot alone.
 *
 * Decision 42 (FR-86, ticket 144) adds one construct to the same rule: a `Leaf` may travel as a
 * prop, but `leaf.text` handed to a component prop - bare or inside a template - may not. The same
 * pairing proves it: 0 in the real tree, and both lifts in the seeded fixture reported while the
 * `Leaf` passed whole is not.
 */
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { ESLint } from "eslint";
import { describe, expect, it } from "vitest";

import config, { NO_DERIVATION_ALLOWLIST } from "../../eslint.config.mjs";
import { LINT_CASE_TIMEOUT_MS } from "../support/timeouts";

const DASHBOARD = resolve(import.meta.dirname, "../..");
const FIXTURE = resolve(DASHBOARD, "tests/support/derives.fixture.tsx");
const EVIDENCE = resolve(DASHBOARD, "../evidence/093/no-derivation.txt");
const LIFTED = resolve(DASHBOARD, "tests/support/leaf-text.fixture.tsx");
const LIFTED_EVIDENCE = resolve(DASHBOARD, "../evidence/144/leaf-text-check.txt");
const LIFT_MESSAGE = "a `Leaf` travels whole";
const RULE = "no-restricted-syntax";

type Finding = { file: string; line: number; message: string };

function findings(results: ESLint.LintResult[]): Finding[] {
  return results.flatMap((result) =>
    result.messages
      .filter((message) => message.ruleId === RULE)
      .map((message) => ({ file: result.filePath, line: message.line, message: message.message })),
  );
}

async function lintAs(eslint: ESLint, source: string, path: string): Promise<Finding[]> {
  return findings(await eslint.lintText(source, { filePath: resolve(DASHBOARD, path) }));
}

describe("no-derivation lint rule", () => {
  it(
    "finds nothing in the real tree and reports both seeded derivations",
    async () => {
      const eslint = new ESLint({ cwd: DASHBOARD });

      const tree = findings(await eslint.lintFiles(["app", "components", "lib"]));
      expect(tree).toEqual([]);

      const seeded = await lintAs(eslint, readFileSync(FIXTURE, "utf8"), "lib/derives.fixture.tsx");
      const seededText = seeded.map((finding) => finding.message).join("\n");
      expect(seeded).toHaveLength(2);
      expect(seededText).toContain("No `-`");
      expect(seededText).toContain("No `.sort()`");

      mkdirSync(dirname(EVIDENCE), { recursive: true });
      writeFileSync(
        EVIDENCE,
        [
          `real tree (app, components, lib): ${tree.length} findings`,
          `tests/support/derives.fixture.tsx linted as lib/: ${seeded.length} findings`,
          ...seeded.map((finding) => `  line ${finding.line}: ${finding.message}`),
          "",
        ].join("\n"),
      );
    },
    LINT_CASE_TIMEOUT_MS, // builds ESLint over the tree: cold start outruns the 5 s default (ticket 228)
  );

  it(
    "finds no leaf.text lifted into a component prop, and fires on the seeded lifts (FR-86)",
    async () => {
      const eslint = new ESLint({ cwd: DASHBOARD });

      const tree = findings(await eslint.lintFiles(["app", "components", "lib"])).filter((finding) =>
        finding.message.includes(LIFT_MESSAGE),
      );
      expect(tree).toEqual([]);

      const seeded = await lintAs(eslint, readFileSync(LIFTED, "utf8"), "components/Lifted.tsx");
      expect(seeded.map((finding) => finding.line)).toEqual([19, 20]);
      for (const finding of seeded) expect(finding.message).toContain(LIFT_MESSAGE);

      // A DOM attribute and a React `key` are not a component's prop.
      const allowed = [
        "<Value leaf={leaf} />",
        "<span data-beat={leaf.text} />",
        "<Row key={leaf.text} leaf={leaf} />",
      ];
      for (const element of allowed) {
        const source = `export const probe = (leaf: any, response: any, path: any) => ${element};\n`;
        expect(await lintAs(eslint, source, "components/Probe.tsx"), element).toEqual([]);
      }

      mkdirSync(dirname(LIFTED_EVIDENCE), { recursive: true });
      writeFileSync(
        LIFTED_EVIDENCE,
        [
          "FR-86, ticket 144: leaf.text lifted into a component prop (decision 42)",
          `real tree (app, components, lib): ${tree.length} findings`,
          `tests/support/leaf-text.fixture.tsx linted as components/: ${seeded.length} findings`,
          ...seeded.map((finding) => `  line ${finding.line}: ${finding.message}`),
          `allowlist: ${JSON.stringify(NO_DERIVATION_ALLOWLIST)} (0 new entries)`,
          "",
        ].join("\n"),
      );
    },
    LINT_CASE_TIMEOUT_MS, // builds ESLint over the tree: cold start outruns the 5 s default (ticket 228)
  );

  it(
    "exempts exactly lib/clock.ts and lib/stream.ts, by name",
    async () => {
      const entries = (config as { name?: string; ignores?: string[] }[]).filter(
        (entry) => entry.name === "harnessed/no-derivation",
      );
      expect(entries).toHaveLength(1);
      expect(entries[0].ignores).toEqual(["lib/clock.ts", "lib/stream.ts", "lib/humanize.ts"]);
      expect(NO_DERIVATION_ALLOWLIST).toEqual(["lib/clock.ts", "lib/stream.ts", "lib/humanize.ts"]);

      const eslint = new ESLint({ cwd: DASHBOARD });
      const source = "export const later = (now: number) => now + 1;\n";
      expect(await lintAs(eslint, source, "lib/clock.ts")).toEqual([]);
      expect(await lintAs(eslint, source, "lib/stream.ts")).toEqual([]);
      expect(await lintAs(eslint, source, "lib/humanize.ts")).toEqual([]);
      expect(await lintAs(eslint, source, "lib/clock.tsx")).toHaveLength(1);
      expect(await lintAs(eslint, source, "lib/other.ts")).toHaveLength(1);
    },
    LINT_CASE_TIMEOUT_MS, // builds ESLint over the tree: cold start outruns the 5 s default (ticket 228)
  );

  it(
    "reports every construct decisions 32 and 34 ban",
    async () => {
      const eslint = new ESLint({ cwd: DASHBOARD });
      const cases = [
        "a - b", "a * b", "a / b", "a % b", "a ** b", "a + b", "i += b", "i++", "i--", "-a", "+a",
        "Number(a)", "parseFloat(a)", "parseInt(a)", "Number.parseInt(a)", "Math.max(a, b)",
        "list.sort()", "list.toSorted()", "list.reverse()", "list.toReversed()", "list.reduce(f)",
        "a.toFixed(2)", "a.toPrecision(2)", "new Intl.NumberFormat()", "localStorage.getItem(a)",
        "sessionStorage.clear()", "indexedDB.open(a)", "navigator.serviceWorker.register(a)",
        "unstable_cache(f)",
      ];
      for (const expression of cases) {
        const source = `export function probe(a: any, b: any, i: any, list: any, f: any) {\n  return ${expression};\n}\n`;
        expect((await lintAs(eslint, source, "lib/probe.ts")).length, expression).toBeGreaterThan(0);
      }
      expect(await lintAs(eslint, "export const last = -1;\n", "lib/probe.ts")).toEqual([]);
      const directive = '"use cache";\n\nexport const cached = "z";\n';
      expect(await lintAs(eslint, directive, "app/cached.ts")).toHaveLength(1);
    },
    LINT_CASE_TIMEOUT_MS, // builds ESLint over the tree: cold start outruns the 5 s default (ticket 228)
  );
});
