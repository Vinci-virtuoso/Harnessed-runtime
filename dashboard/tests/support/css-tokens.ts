/**
 * Reads `app/tokens.css` as the browser ships it, with no CSS package: the four theme blocks, the
 * surface layer's glass defaults and its one solid block, and the arithmetic of a contrast ratio.
 * The arithmetic lives here, in the tests, never in `app/`, `components/` or `lib/` (decision 32).
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import UI_CONTRACT from "../fixtures/ui-contract.json";

export const DASHBOARD = resolve(import.meta.dirname, "../..");
export const TOKENS_PATH = resolve(DASHBOARD, "app/tokens.css");


export const THEMES = ["dark", "light"] as const;
export type Theme = (typeof THEMES)[number];

/** The twelve glass properties of design.md's Glass table, in the table's order. */
export const GLASS_NAMES = [
  "glass-field",
  "glass-sidebar",
  "glass-surface",
  "glass-raised",
  "glass-sunken",
  "glass-edge",
  "glass-hairline",
  "glass-hairline-strong",
  "glass-frost",
  "glass-depth",
  "glass-primary",
  "glass-primary-glow",
] as const;

/** The surface layer a component reads. */
export const SURFACE_NAMES = [
  "surface-field",
  "surface-sidebar",
  "surface-panel",
  "surface-raised",
  "surface-sunken",
  "surface-edge",
  "surface-hairline",
  "surface-hairline-strong",
  "surface-frost",
  "surface-depth",
  "surface-primary",
  "surface-primary-glow",
] as const;

/** The four selectors, in the stylesheet's order, each the opening of one rule's prelude. */
export const SOLID_PRELUDES = [
  "@supports not ((backdrop-filter: blur(1px)) or (-webkit-backdrop-filter: blur(1px)))",
  "@media (prefers-reduced-transparency: reduce)",
  "@media (prefers-contrast: more)",
  ':root:has([data-surface="solid"])',
] as const;

export function readTokens(): string {
  return stripComments(readFileSync(TOKENS_PATH, "utf8"));
}

export function stripComments(css: string): string {
  return css.replace(/\/\*[\s\S]*?\*\//g, "");
}

/** The text between the braces that open at `css[open]`, and the index just past the close. */
function balanced(css: string, open: number): { inner: string; end: number } {
  let depth = 0;
  for (let i = open; i < css.length; i += 1) {
    if (css[i] === "{") depth += 1;
    if (css[i] === "}") {
      depth -= 1;
      if (depth === 0) return { inner: css.slice(open + 1, i), end: i + 1 };
    }
  }
  throw new Error("unbalanced braces");
}

/** Every top-level rule or at-rule as `{ prelude, inner }`. */
export function topLevel(css: string): { prelude: string; inner: string }[] {
  const out: { prelude: string; inner: string }[] = [];
  let at = 0;
  for (;;) {
    const open = css.indexOf("{", at);
    if (open < 0) return out;
    const prelude = css.slice(at, open).trim();
    const { inner, end } = balanced(css, open);
    out.push({ prelude, inner });
    at = end;
  }
}

/** `--name: value;` pairs of a declaration list (the values may hold commas and parentheses). */
export function declarations(body: string): Map<string, string> {
  const map = new Map<string, string>();
  for (const m of body.matchAll(/(--[a-z0-9-]+)\s*:\s*([^;]+);/g)) map.set(m[1], m[2].replace(/\s+/g, " ").trim());
  return map;
}

/** The four theme blocks' declarations, keyed by the theme they paint. */
export function themeBlocks(css: string): { label: string; theme: Theme; decls: Map<string, string> }[] {
  const rules = topLevel(css);
  const find = (prelude: string) => {
    const rule = rules.find((r) => r.prelude === prelude);
    if (!rule) throw new Error(`no ${prelude}`);
    return rule;
  };
  const media = find("@media (prefers-color-scheme: light)");
  const inner = topLevel(media.inner).find((r) => r.prelude === ':root:not([data-theme="dark"])');
  if (!inner) throw new Error("no light-by-system rule");
  return [
    { label: ":root", theme: "dark", decls: declarations(rules.find((r) => r.prelude === ":root")!.inner) },
    { label: "prefers-color-scheme: light", theme: "light", decls: declarations(inner.inner) },
    { label: ':root[data-theme="light"]', theme: "light", decls: declarations(find(':root[data-theme="light"]').inner) },
    { label: ':root[data-theme="dark"]', theme: "dark", decls: declarations(find(':root[data-theme="dark"]').inner) },
  ];
}

/** The `:root` rule that declares the `--surface-*` defaults. */
export function surfaceDefaults(css: string): Map<string, string> {
  const rule = topLevel(css).find((r) => r.prelude === ":root" && r.inner.includes("--surface-field"));
  if (!rule) throw new Error("no :root surface layer");
  return declarations(rule.inner);
}

/** The solid declaration list under each of the four selectors, keyed by prelude. */
export function solidBlocks(css: string): Map<string, string> {
  const out = new Map<string, string>();
  for (const rule of topLevel(css)) {
    if (!SOLID_PRELUDES.includes(rule.prelude as (typeof SOLID_PRELUDES)[number])) continue;
    const inner =
      rule.prelude === SOLID_PRELUDES[3]
        ? rule.inner
        : (topLevel(rule.inner).find((r) => r.prelude === ":root")?.inner ?? "");
    out.set(rule.prelude, [...declarations(inner)].map(([k, v]) => `${k}: ${v}`).join("; "));
  }
  return out;
}

/** Frozen glass values, independent of the stylesheet being tested. */
export function glassTable(): Record<string, Record<Theme, string>> {
  return UI_CONTRACT.glass;
}

export type Rgba = { r: number; g: number; b: number; a: number };

export function parseColor(value: string): Rgba {
  const v = value.trim();
  const hex = /^#([0-9a-f]{6})$/i.exec(v);
  if (hex) {
    const n = parseInt(hex[1], 16);
    return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255, a: 1 };
  }
  const fn = /^rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+)\s*)?\)$/.exec(v);
  if (fn) return { r: Number(fn[1]), g: Number(fn[2]), b: Number(fn[3]), a: fn[4] === undefined ? 1 : Number(fn[4]) };
  throw new Error(`not a colour: ${value}`);
}

/** `top` over `bottom`, source-over. */
export function over(top: Rgba, bottom: Rgba): Rgba {
  const a = top.a + bottom.a * (1 - top.a);
  const mix = (t: number, b: number) => (t * top.a + b * bottom.a * (1 - top.a)) / a;
  return { r: mix(top.r, bottom.r), g: mix(top.g, bottom.g), b: mix(top.b, bottom.b), a };
}

function channel(c: number): number {
  const s = c / 255;
  return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
}

export function luminance(c: Rgba): number {
  return 0.2126 * channel(c.r) + 0.7152 * channel(c.g) + 0.0722 * channel(c.b);
}

export function ratio(a: Rgba, b: Rgba): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

/** The opaque colour stops of a `glass-field` value: each `#rrggbb` that is not a fade-out edge. */
export function fieldStops(field: string): Rgba[] {
  return [...field.matchAll(/#[0-9a-fA-F]{6}\b/g)].map((m) => parseColor(m[0]));
}
