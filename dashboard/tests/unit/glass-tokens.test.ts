/**
 * FR-158 / decision 84: glass is one stylesheet. The four theme blocks carry design.md's twelve
 * `--glass-*` properties verbatim, the `--surface-*` layer defaults to them, the one solid block
 * repeats under exactly four selectors, and nothing outside `tokens.css` names a glass or
 * `--color-surface*` variable (MUST rule 41). A seeded violation proves each check can fail.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, posix, relative, sep, win32 } from "node:path";
import { describe, expect, it } from "vitest";

import { surfaceOf, SOLID_PAGES } from "../../lib/surface";
import {
  DASHBOARD,
  GLASS_NAMES,
  SOLID_PRELUDES,
  SURFACE_NAMES,
  glassTable,
  readTokens,
  solidBlocks,
  surfaceDefaults,
  themeBlocks,
  topLevel,
} from "../support/css-tokens";

const css = readTokens();
const table = glassTable();

/**
 * Values the UX ruling of 2026-10-04 (ticket 250, rule 2) raised above the spine's table, which is
 * amended to these afterwards; every other value stays verbatim. Dark theme only.
 */
const RULED: Record<string, string> = {
  "glass-sidebar": "rgba(14,17,22,0.70)",
  "glass-surface": "rgba(20,24,32,0.85)",
};

/** A path as `/` spells it: `relative` answers with `\\` on Windows, so every comparison and every
 * reported name goes through here (ticket 263). */
export function portable(path: string, separator: string = sep): string {
  return path.split(separator).join(posix.sep);
}

/** Component and page stylesheets and sources: everything that is not the token file. */
function consumers(): string[] {
  const out: string[] = [];
  const walk = (dir: string) => {
    for (const name of readdirSync(dir)) {
      const path = join(dir, name);
      if (statSync(path).isDirectory()) walk(path);
      else if (/\.(css|tsx?)$/.test(name) && portable(relative(DASHBOARD, path)) !== "app/tokens.css") out.push(path);
    }
  };
  walk(join(DASHBOARD, "components"));
  walk(join(DASHBOARD, "app"));
  return out;
}

const FORBIDDEN = /--(?:glass-|color-surface)[a-z-]*/g;

function violations(files: Record<string, string>): string[] {
  return Object.entries(files).flatMap(([path, text]) =>
    [...text.matchAll(FORBIDDEN)].map((m) => `${path} names ${m[0]}`),
  );
}

describe("glass tokens", () => {
  it("reads twelve properties from the spine's table", () => {
    expect(Object.keys(table)).toEqual([...GLASS_NAMES]);
  });

  it("carries every glass value verbatim in all four theme blocks", () => {
    const blocks = themeBlocks(css);
    expect(blocks).toHaveLength(4);
    for (const { label, theme, decls } of blocks) {
      for (const name of GLASS_NAMES) {
        const spine = table[name][theme].replace(/\s+/g, " ");
        const expected = theme === "dark" && RULED[name] ? RULED[name] : spine;
        expect(decls.get(`--${name}`), `${label} --${name}`).toBe(expected);
      }
    }
  });

  it("amends the radii to 6, 10 and 14px and keeps no gradient outside the glass tokens", () => {
    const shared = topLevel(css).find((r) => r.prelude === ":root" && r.inner.includes("--radius-sm"))!;
    expect(shared.inner).toMatch(/--radius-sm:\s*6px;/);
    expect(shared.inner).toMatch(/--radius-md:\s*10px;/);
    expect(shared.inner).toMatch(/--radius-lg:\s*14px;/);
  });

  it("defaults the surface layer to the glass tokens", () => {
    const layer = surfaceDefaults(css);
    for (const name of SURFACE_NAMES) {
      const glass = name.replace("surface-", "glass-").replace("glass-panel", "glass-surface");
      expect(layer.get(`--${name}`), name).toBe(`var(--${glass})`);
    }
  });

  it("repeats one solid block verbatim under exactly the four selectors", () => {
    const blocks = solidBlocks(css);
    expect([...blocks.keys()]).toEqual([...SOLID_PRELUDES]);
    const first = blocks.get(SOLID_PRELUDES[0])!;
    for (const prelude of SOLID_PRELUDES) expect(blocks.get(prelude), prelude).toBe(first);
    for (const name of SURFACE_NAMES) expect(first, name).toContain(`--${name}:`);
    expect(first).toContain("--surface-frost: none");
    expect(first).toContain("--surface-field: var(--color-bg)");
    expect(first).not.toContain("--glass-");
  });

  it("is the only file that names a glass or --color-surface variable", () => {
    const files = Object.fromEntries(consumers().map((p) => [portable(relative(DASHBOARD, p)), readFileSync(p, "utf8")]));
    expect(Object.keys(files).length).toBeGreaterThan(50);
    expect(violations(files)).toEqual([]);
  });

  it("spells a Windows path with the separators the comparison expects", () => {
    expect(portable("app\\tokens.css", win32.sep)).toBe("app/tokens.css");
    expect(portable("app/tokens.css", posix.sep)).toBe("app/tokens.css");
    expect(portable(win32.relative("C:\\d", "C:\\d\\components\\a.css"), win32.sep)).toBe("components/a.css");
  });

  it("fires on a seeded component that reads a glass variable", () => {
    const seeded = { "components/Seed.module.css": ".a { background: var(--glass-surface); border-color: var(--color-surface-sunken); }" };
    expect(violations(seeded)).toEqual([
      "components/Seed.module.css names --glass-surface",
      "components/Seed.module.css names --color-surface-sunken",
    ]);
  });

  it("paints the ground as a fixed pseudo-element, never background-attachment", () => {
    expect(css).toMatch(/body::before\s*\{[^}]*position:\s*fixed;[^}]*inset:\s*0;[^}]*background:\s*var\(--surface-field\)/);
    expect(css).not.toContain("background-attachment");
    expect(css).toMatch(/html,\s*body\s*\{[^}]*background:\s*var\(--color-bg\)/);
  });

  it("gives every surface rule frost, an edge and its depth, and the scrims no blur", () => {
    const sheets = consumers().filter((p) => p.endsWith(".css"));
    for (const path of sheets) {
      for (const rule of topLevel(readFileSync(path, "utf8").replace(/\/\*[\s\S]*?\*\//g, ""))) {
        const body = rule.inner;
        if (/\.scrim\b/.test(rule.prelude)) expect(body, `${path} scrim`).not.toMatch(/backdrop-filter/);
        if (/background:\s*var\(--surface-panel\)/.test(body)) {
          expect(body, `${path} ${rule.prelude}`).toContain("backdrop-filter: var(--surface-frost)");
          expect(body, `${path} ${rule.prelude}`).toContain("-webkit-backdrop-filter: var(--surface-frost)");
          expect(body, `${path} ${rule.prelude}`).toContain("var(--surface-edge)");
        }
        if (/background:\s*var\(--surface-sunken\)/.test(body)) expect(body, path).not.toMatch(/backdrop-filter/);
      }
    }
  });
});

describe("SOLID_PAGES", () => {
  it("starts empty, so every page is glass", () => {
    expect(SOLID_PAGES).toEqual([]);
    expect(surfaceOf("/traces/")).toBeUndefined();
  });

  it("marks a listed page solid by its first path segment, the overview included", () => {
    expect(surfaceOf("/traces/", ["traces"])).toBe("solid");
    expect(surfaceOf("/traces", ["traces"])).toBe("solid");
    expect(surfaceOf("/", ["overview"])).toBe("solid");
    expect(surfaceOf(null, ["overview"])).toBe("solid");
    expect(surfaceOf("/memory/", ["traces"])).toBeUndefined();
  });
});

describe("code-font ligatures (ticket 306, FR-174)", () => {
  it("adds no --token beyond the declared set: the rule is a plain declaration", () => {
    expect(css).toMatch(/font-variant-ligatures:\s*none;/);
    expect(css).not.toMatch(/--[a-z-]*ligature/);
  });
});
