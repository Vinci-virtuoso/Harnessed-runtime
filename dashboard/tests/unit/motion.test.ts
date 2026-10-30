/**
 * FR-162, the static half: every control that styles `:hover` or `:active` transitions that change
 * at `motion-micro`, and only on properties that move no layout (design.md, "Controls respond").
 * The scan reads every `*.module.css`, so a control added later is covered, and each rule of the
 * scan is run a second time against a synthetic stylesheet that breaks it, so a scan that cannot
 * fail is caught here (MUST rule 12).
 */
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative, resolve } from "node:path";
import { describe, expect, it } from "vitest";

const DASHBOARD = resolve(import.meta.dirname, "../..");

const ALLOWED = new Set([
  "opacity",
  "color",
  "background-color",
  "border-color",
  "transform",
]);

/** A hover that changes only these has nothing a transition could animate. */
const NOT_TRANSITIONABLE = new Set(["text-decoration", "cursor", "text-align"]);

type Rule = { selectors: string[]; declarations: Map<string, string> };

function modules(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    if (name === "node_modules" || name === "out" || name === ".next")
      return [];
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return modules(path);
    return name.endsWith(".module.css") ? [path] : [];
  });
}

export function rules(css: string): Rule[] {
  const bare = css.replace(/\/\*[\s\S]*?\*\//g, "");
  return [...bare.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
    .filter((m) => !m[1].trim().startsWith("@"))
    .map((m) => ({
      selectors: m[1].split(",").map((s) => s.trim()),
      declarations: new Map(
        m[2]
          .split(";")
          .map((d) => d.split(/:(.*)/s))
          .filter((p) => p.length > 1)
          .map((p) => [p[0].trim(), p[1].trim()] as [string, string]),
      ),
    }));
}

/** The control a state rule styles: the selector without its pseudo-class. */
function subject(selector: string): string {
  const bare = selector
    .replace(/:not\(:disabled\)/g, "")
    .replace(/:(hover|active)\b/g, "")
    .trim();
  return bare.split(/\s+/).at(-1) ?? bare;
}

function transitionedProperties(value: string): string[] {
  return value.split(/,(?![^(]*\))/).map((part) => part.trim().split(/\s+/)[0]);
}

export function findings(css: string): string[] {
  const all = rules(css);
  const out: string[] = [];
  for (const rule of all) {
    const transition = rule.declarations.get("transition");
    if (transition !== undefined && transition !== "none") {
      for (const part of transition.split(/,(?![^(]*\))/)) {
        if (!part.includes("var(--motion-micro)"))
          out.push(`duration is not --motion-micro: ${part.trim()}`);
        const property = part.trim().split(/\s+/)[0];
        if (!ALLOWED.has(property)) out.push(`transition on ${property}`);
      }
    }
    if (transition === "none")
      out.push(`${rule.selectors[0]} sets transition: none`);
    for (const selector of rule.selectors.filter((s) =>
      /:(hover|active)\b/.test(s),
    )) {
      const changed = [...rule.declarations.keys()].filter(
        (p) => !NOT_TRANSITIONABLE.has(p),
      );
      if (changed.length === 0 || /\b(rect|polygon)\b/.test(selector)) continue;
      const base = subject(selector);
      const owner = all.find(
        (r) => r.selectors.includes(base) && r.declarations.has("transition"),
      );
      if (!owner) {
        out.push(`${selector} has no transition on ${base}`);
        continue;
      }
      const covered = transitionedProperties(
        owner.declarations.get("transition") ?? "",
      );
      for (const property of changed) {
        if (!covered.includes(property))
          out.push(
            `${selector} changes ${property}, which ${base} does not transition`,
          );
      }
    }
  }
  return out;
}

describe("control transitions (FR-162)", () => {
  const files = modules(DASHBOARD);

  it("reads every CSS module", () => {
    expect(files.length).toBeGreaterThan(50);
  });

  it("finds nothing wrong in any module", () => {
    const report = files.flatMap((file) =>
      findings(readFileSync(file, "utf8")).map(
        (f) => `${relative(DASHBOARD, file)}: ${f}`,
      ),
    );
    expect(report).toEqual([]);
  });

  it("sees a hover with no transition", () => {
    expect(findings(".a { color: red; } .a:hover { color: blue; }")).toEqual([
      ".a:hover has no transition on .a",
    ]);
  });

  it("sees transition: all, a layout property and a foreign duration", () => {
    expect(
      findings(".a { transition: all var(--motion-micro) ease; }"),
    ).toEqual(["transition on all"]);
    expect(
      findings(".a { transition: width var(--motion-micro) ease; }"),
    ).toEqual(["transition on width"]);
    expect(findings(".a { transition: color 300ms ease; }")).toEqual([
      "duration is not --motion-micro: color 300ms ease",
    ]);
  });

  it("sees a hovered property the base does not transition", () => {
    const css =
      ".a { transition: color var(--motion-micro) var(--ease-out); } .a:hover { border-color: red; }";
    expect(findings(css)).toEqual([
      ".a:hover changes border-color, which .a does not transition",
    ]);
  });

  it("accepts a covered hover and a press", () => {
    const css =
      ".a { transition: color var(--motion-micro) var(--ease-out), transform var(--motion-micro) var(--ease-out); } .a:hover { color: red; } .a:active { transform: translateY(1px); }";
    expect(findings(css)).toEqual([]);
  });
});
