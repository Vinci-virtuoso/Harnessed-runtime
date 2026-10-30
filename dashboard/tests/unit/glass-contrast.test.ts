/**
 * G-1: glass changes no text, figure or status colour, so every pair must still hold measured on
 * the glass over the field's brightest part, from the shipped stylesheet and not from a sentence
 * (decision 84). 4.5:1 for the three text greys, the accent as link text and each status word
 * (also on its own `*-subtle` over glass-surface, UX ruling 2026-10-04); 3:1 for figures and the focus ring. `color-fg-faint`
 * is exempt. A failing pair is the operator's to rule on: this test never edits a colour.
 */
import { mkdirSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

import {
  DASHBOARD,
  THEMES,
  fieldStops,
  luminance,
  over,
  parseColor,
  ratio,
  readTokens,
  themeBlocks,
  type Rgba,
  type Theme,
} from "../support/css-tokens";

const css = readTokens();
const blocks = themeBlocks(css);

function theme(t: Theme) {
  return blocks.find((b) => b.theme === t)!.decls;
}

const TEXT = ["color-fg", "color-fg-secondary", "color-fg-muted", "color-accent", "color-pass", "color-fail", "color-warn"];
const STATUS = [
  ["color-pass", "color-pass-subtle"],
  ["color-fail", "color-fail-subtle"],
  ["color-warn", "color-warn-subtle"],
];
const SURFACES = ["glass-sidebar", "glass-surface", "glass-raised"] as const;

type Row = { theme: Theme; pair: string; ratio: number; floor: number };

function measure(t: Theme): Row[] {
  const d = theme(t);
  const color = (name: string) => parseColor(d.get(`--${name}`)!);
  const stops = fieldStops(d.get("--glass-field")!);
  expect(stops.length).toBeGreaterThanOrEqual(5);
  // The field's brightest stop, as the ticket and G-1 specify.
  const brightest = [...stops].sort((a, b) => luminance(b) - luminance(a))[0];
  const extremes = [brightest];
  const rows: Row[] = [];
  const grounds: { name: string; at: (field: Rgba) => Rgba }[] = [
    ...SURFACES.map((s) => ({ name: s === "glass-raised" ? "glass-raised over glass-surface" : s, at: (f: Rgba) => (s === "glass-raised" ? over(color2(d, s), over(color2(d, "glass-surface"), f)) : over(color2(d, s), f)) })),
    { name: "glass-sunken over glass-surface", at: (f) => over(color2(d, "glass-sunken"), over(color2(d, "glass-surface"), f)) },
  ];
  for (const field of extremes) {
    for (const g of grounds) {
      const ground = g.at(field);
      // UX ruling 2026-10-04, rule 2: raised carries fg-secondary, accent and the status words (nav item,
      // button, selected segment); the muted grey never sits on it, so it is not measured there.
      const onRaised = g.name.startsWith("glass-raised");
      for (const name of TEXT.filter((n) => !(onRaised && n === "color-fg-muted"))) rows.push({ theme: t, pair: `${name} on ${g.name}`, ratio: ratio(over(color(name), ground), ground), floor: 4.5 });
      rows.push({ theme: t, pair: `figure (color-fg) on ${g.name}`, ratio: ratio(color("color-fg"), ground), floor: 3 });
      rows.push({ theme: t, pair: `focus ring (color-accent) on ${g.name}`, ratio: ratio(color("color-accent"), ground), floor: 3 });
    }
    for (const [word, subtle] of STATUS) {
      // A status ground sits inside a panel (UX ruling 2026-10-04, rule 3): over glass-surface over the field.
      const ground = over(color(subtle), over(color2(d, "glass-surface"), field));
      rows.push({ theme: t, pair: `${word} on ${subtle} over glass-surface`, ratio: ratio(over(color(word), ground), ground), floor: 4.5 });
    }
  }
  return rows;
}

function color2(d: Map<string, string>, name: string): Rgba {
  return parseColor(d.get(`--${name}`)!);
}

describe("glass contrast", () => {
  const rows = THEMES.flatMap(measure);

  it("measures a real number of pairs in each theme", () => {
    expect(rows.filter((r) => r.theme === "dark").length).toBeGreaterThanOrEqual(35);
    expect(rows.filter((r) => r.theme === "light").length).toBeGreaterThanOrEqual(35);
  });

  it("holds every pair at its floor in both themes", () => {
    const failing = rows.filter((r) => r.ratio < r.floor).map((r) => `${r.theme}: ${r.pair} ${r.ratio.toFixed(2)} < ${r.floor}`);
    expect(failing).toEqual([]);
  });

  it("can fail: a glass surface that is far too transparent breaks the text floor", () => {
    const d = new Map(theme("light"));
    const white: Rgba = { r: 255, g: 255, b: 255, a: 1 };
    const ground = over(parseColor("rgba(255,255,255,0.1)"), white);
    expect(ratio(parseColor(d.get("--color-fg-faint")!), ground)).toBeLessThan(4.5);
  });

  it("writes the table the operator reads", () => {
    const dir = resolve(DASHBOARD, "../evidence/250");
    mkdirSync(dir, { recursive: true });
    const lines = rows.map((r) => `${r.theme}\t${r.pair}\t${r.ratio.toFixed(2)}\t>= ${r.floor}`);
    writeFileSync(resolve(dir, "contrast.tsv"), ["theme\tpair\tratio\tfloor", ...lines].join("\n") + "\n");
    expect(lines.length).toBe(rows.length);
  });
});
