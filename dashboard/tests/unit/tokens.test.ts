/**
 * The two themes declare the same tokens (FR-43, FR-61). The names come from the colour table in
 * `.spec/ux/design.md`, so a token added to the design and forgotten in either theme fails here,
 * and a seeded omission proves the check can fail at all (MUST rule 12).
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const DASHBOARD = resolve(import.meta.dirname, "../..");
const TOKENS_CSS = readFileSync(resolve(DASHBOARD, "app/tokens.css"), "utf8");
const DESIGN_MD = readFileSync(resolve(DASHBOARD, "../.spec/ux/design.md"), "utf8");

/** Review-only tokens the design says never ship. */
const REVIEW_ONLY = new Set(["color-gap"]);

const LIGHT_QUERY = "(prefers-color-scheme: light)";
const LIGHT_BY_SYSTEM = ':root:not([data-theme="dark"])';

/** A row `| \`color-pass\` / \`-subtle\` | ...` yields color-pass and color-pass-subtle. */
function designTokens(markdown: string): Set<string> {
  const section = markdown.split("## Color tokens")[1].split("\n## ")[0];
  const names = new Set<string>();
  for (const row of section.matchAll(/^\| `(color-[a-z-]+)`(?: \/ `(-[a-z-]+)`)? \|/gm)) {
    names.add(row[1]);
    if (row[2]) names.add(`${row[1]}${row[2]}`);
  }
  for (const name of REVIEW_ONLY) names.delete(name);
  return names;
}

function withoutComments(css: string): string {
  return css.replace(/\/\*[\s\S]*?\*\//g, "");
}

/** The declarations of the first rule whose selector is exactly `selector`. */
function block(css: string, selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = new RegExp(`(?:^|[{};])\\s*${escaped}\\s*\\{([^{}]*)\\}`).exec(css);
  if (!match) throw new Error(`no rule for ${selector}`);
  return match[1];
}

function mediaBlock(css: string, query: string, selector: string): string {
  const start = css.indexOf(`@media ${query}`);
  if (start < 0) throw new Error(`no @media ${query}`);
  return block(css.slice(start + `@media ${query}`.length).replace(/^\s*\{/, ";"), selector);
}

function colorNames(declarations: string): Set<string> {
  return new Set([...declarations.matchAll(/--(color-[a-z-]+)\s*:/g)].map((m) => m[1]));
}

function themes(source: string): Record<string, Set<string>> {
  const css = withoutComments(source);
  return {
    ":root (dark)": colorNames(block(css, ":root")),
    "prefers-color-scheme: light": colorNames(mediaBlock(css, LIGHT_QUERY, LIGHT_BY_SYSTEM)),
    '[data-theme="light"]': colorNames(block(css, ':root[data-theme="light"]')),
    '[data-theme="dark"]': colorNames(block(css, ':root[data-theme="dark"]')),
  };
}

/** Every theme's missing and surplus names, one line each; empty when all agree. */
function mismatches(css: string, expected: Set<string>): string[] {
  const lines: string[] = [];
  for (const [theme, names] of Object.entries(themes(css))) {
    for (const name of expected) if (!names.has(name)) lines.push(`${theme} lacks --${name}`);
    for (const name of names) if (!expected.has(name)) lines.push(`${theme} adds --${name}`);
  }
  return lines;
}

/** `css` with the first declaration of `name` removed from the rule `declarations`. */
function drop(css: string, declarations: string, name: string): string {
  const pruned = declarations.replace(new RegExp(`--${name}\\s*:[^;]*;`), "");
  expect(pruned).not.toBe(declarations);
  return css.replace(declarations, pruned);
}

describe("tokens.css", () => {
  const expected = designTokens(DESIGN_MD);
  const css = withoutComments(TOKENS_CSS);

  it("reads the design's colour tokens", () => {
    expect(expected.size).toBeGreaterThanOrEqual(20);
    expect(expected).toContain("color-bg");
    expect(expected).toContain("color-fail-subtle");
    expect(expected).not.toContain("color-gap");
  });

  it("declares the identical set in both themes and both explicit choices", () => {
    expect(mismatches(css, expected)).toEqual([]);
  });

  it("fails when a name is missing from the light theme", () => {
    const light = mediaBlock(css, LIGHT_QUERY, LIGHT_BY_SYSTEM);
    expect(mismatches(drop(css, light, "color-warn-subtle"), expected)).toEqual([
      "prefers-color-scheme: light lacks --color-warn-subtle",
    ]);
  });

  it("fails when a name is missing from the dark theme", () => {
    const dark = block(css, ":root");
    expect(mismatches(drop(css, dark, "color-accent"), expected)).toEqual([
      ":root (dark) lacks --color-accent",
    ]);
  });

  it("declares the motion tokens a control's state change uses (FR-162)", () => {
    expect(css).toMatch(/--motion-micro:\s*150ms;/);
    expect(css).toMatch(/--ease-out:\s*cubic-bezier\(0\.25,\s*1,\s*0\.5,\s*1\);/);
  });

  it("turns ligatures off in the code font so command text keeps its hyphens (FR-174)", () => {
    expect(css).toMatch(/\bcode\b[^{}]*\{[^}]*font-variant-ligatures:\s*none;/);
  });
});
