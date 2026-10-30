// @vitest-environment jsdom
/**
 * FR-157, decision 85: the theme toggle, `lib/theme.ts` and `public/theme-init.js`.
 *
 * jsdom evaluates no stylesheet, so the computed `--color-bg` is read by resolving which block of
 * `app/tokens.css` wins for a system scheme and an `html[data-theme]` value - the same four blocks
 * the browser cascades - and `theme.spec.ts` measures the same thing in a real browser.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { ESLint } from "eslint";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ThemeToggle } from "../../components/ThemeToggle";
import { remember, THEME_KEY } from "../../lib/theme";

const DASHBOARD = resolve(import.meta.dirname, "../..");
const TOKENS = readFileSync(resolve(DASHBOARD, "app/tokens.css"), "utf8");
const INIT = readFileSync(resolve(DASHBOARD, "public/theme-init.js"), "utf8");
const html = document.documentElement;

/** The `--color-bg` of the first block with this selector (and, if given, inside this media query). */
function bgOf(selector: string, inMedia: boolean): string {
  const source = inMedia ? TOKENS.slice(TOKENS.indexOf("@media (prefers-color-scheme: light)")) : TOKENS;
  const at = source.indexOf(`${selector} {`);
  expect(at, selector).toBeGreaterThanOrEqual(0);
  return /--color-bg:\s*(#[0-9a-f]+)/i.exec(source.slice(at))![1].toLowerCase();
}

/** What the cascade gives `--color-bg` for a system scheme and an `html[data-theme]` value. */
function computedBg(scheme: "dark" | "light", attribute: string | undefined): string {
  if (attribute === "light") return bgOf(':root[data-theme="light"]', false);
  if (attribute === "dark") return bgOf(':root[data-theme="dark"]', false);
  return scheme === "light" ? bgOf(':root:not([data-theme="dark"])', true) : bgOf(":root", false);
}

function system(scheme: "dark" | "light") {
  const listeners = new Set<() => void>();
  window.matchMedia = ((query: string) => ({
    matches: scheme === "light" && query.includes("light"),
    media: query,
    addEventListener: (_: string, fn: () => void) => listeners.add(fn),
    removeEventListener: (_: string, fn: () => void) => listeners.delete(fn),
  })) as unknown as typeof window.matchMedia;
}

const runInit = () => new Function(INIT)();

beforeEach(() => {
  html.removeAttribute("data-theme");
  window.localStorage.clear();
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("ThemeToggle", () => {
  it("is one real button named for the action, with the icon of the theme showing", () => {
    system("dark");
    render(<ThemeToggle />);
    const button = screen.getByRole("button", { name: "Switch to light theme" });
    expect(button.tagName).toBe("BUTTON");
    expect(button.getAttribute("title")).toBe("Switch to light theme");
    expect(button.querySelector("svg")?.getAttribute("data-icon")).toBe("sun");
    expect(button.querySelector("svg")?.getAttribute("width")).toBe("16");
    cleanup();

    system("light");
    render(<ThemeToggle />);
    const light = screen.getByRole("button", { name: "Switch to dark theme" });
    expect(light.getAttribute("title")).toBe("Switch to dark theme");
    expect(light.querySelector("svg")?.getAttribute("data-icon")).toBe("moon");
  });

  it("a press sets data-theme, stores the choice under harnessed.theme, and flips the icon", async () => {
    system("dark");
    render(<ThemeToggle />);
    fireEvent.click(screen.getByRole("button"));
    expect(html.dataset.theme).toBe("light");
    expect(window.localStorage.getItem("harnessed.theme")).toBe("light");
    expect(THEME_KEY).toBe("harnessed.theme");
    await act(async () => undefined);
    expect(screen.getByRole("button").getAttribute("title")).toBe("Switch to dark theme");
    fireEvent.click(screen.getByRole("button"));
    expect(html.dataset.theme).toBe("dark");
    expect(window.localStorage.getItem("harnessed.theme")).toBe("dark");
  });

  it("with no stored choice sets no data-theme until pressed", () => {
    system("light");
    render(<ThemeToggle />);
    expect(html.hasAttribute("data-theme")).toBe(false);
  });

  it("still switches the theme when localStorage throws, and nothing errors", () => {
    system("dark");
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    render(<ThemeToggle />);
    expect(() => fireEvent.click(screen.getByRole("button"))).not.toThrow();
    expect(html.dataset.theme).toBe("light");
    expect(() => remember("dark")).not.toThrow();
  });

  it("makes no request and announces nothing", () => {
    system("dark");
    const fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    const { container } = render(<ThemeToggle />);
    fireEvent.click(screen.getByRole("button"));
    expect(fetcher).not.toHaveBeenCalled();
    expect(container.querySelector("[aria-live], [role=status], [role=alert]")).toBeNull();
    vi.unstubAllGlobals();
  });

  it("transitions only its own hover and press, in its module", () => {
    const css = readFileSync(resolve(DASHBOARD, "components/ThemeToggle.module.css"), "utf8");
    expect(css).not.toMatch(/transition:\s*(?:all|none)/);
    expect(css).not.toMatch(/--(glass|color-surface)/);
  });

  it("holds the swap marker only for the press and leaves none behind", () => {
    system("dark");
    render(<ThemeToggle />);
    fireEvent.click(screen.getByRole("button"));
    expect(html.dataset.themeSwap).toBeUndefined();
    expect(html.dataset.theme).toBe("light");
  });
});

describe("the computed --color-bg, each system scheme with and without a stored choice", () => {
  const cases: [("dark" | "light"), (string | null), string][] = [
    ["dark", null, "#0b0d10"],
    ["light", null, "#f7f8fa"],
    ["dark", "light", "#f7f8fa"],
    ["light", "dark", "#0b0d10"],
    ["dark", "dark", "#0b0d10"],
    ["light", "light", "#f7f8fa"],
  ];
  it.each(cases)("system %s, stored %s -> %s", (scheme, stored, expected) => {
    system(scheme);
    if (stored !== null) window.localStorage.setItem(THEME_KEY, stored);
    runInit();
    expect(computedBg(scheme, html.dataset.theme)).toBe(expected);
    expect(html.hasAttribute("data-theme")).toBe(stored !== null);
  });

  it("theme-init ignores an unknown value and a throwing store", () => {
    window.localStorage.setItem(THEME_KEY, "sepia");
    runInit();
    expect(html.hasAttribute("data-theme")).toBe(false);
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    expect(runInit).not.toThrow();
    expect(html.hasAttribute("data-theme")).toBe(false);
  });
});

describe("the label before hydration (FR-174)", () => {
  const server = () =>
    '<button aria-label="Switch to light theme" title="Switch to light theme" ' +
    'data-label-light="Switch to dark theme" data-label-dark="Switch to light theme"></button>';
  afterEach(() => {
    document.body.innerHTML = "";
  });

  it.each([
    ["light", "Switch to dark theme"],
    ["dark", "Switch to light theme"],
  ])("a stored %s names the action %s from the data-label attributes", (stored, label) => {
    document.body.innerHTML = server();
    window.localStorage.setItem(THEME_KEY, stored);
    runInit();
    const button = document.body.querySelector("button")!;
    expect(button.getAttribute("aria-label")).toBe(label);
    expect(button.getAttribute("title")).toBe(label);
  });

  it("leaves the server label when nothing is stored", () => {
    document.body.innerHTML = server();
    runInit();
    expect(document.body.querySelector("button")!.getAttribute("aria-label")).toBe("Switch to light theme");
  });

  it("the toggle carries both labels for the script", () => {
    system("dark");
    render(<ThemeToggle />);
    const button = screen.getByRole("button");
    expect(button.getAttribute("data-label-light")).toBe("Switch to dark theme");
    expect(button.getAttribute("data-label-dark")).toBe("Switch to light theme");
  });
});

describe("the head script and the lint allowlist", () => {
  it("is a plain synchronous script, first in head, with no inline script", () => {
    const layout = readFileSync(resolve(DASHBOARD, "app/layout.tsx"), "utf8");
    expect(layout).toContain('<script src="/theme-init.js"></script>');
    expect(layout).not.toMatch(/async|defer|dangerouslySetInnerHTML/);
    expect(layout.indexOf("<head>")).toBeLessThan(layout.indexOf("<body>"));
    expect(INIT).not.toMatch(/fetch|XMLHttpRequest|sendBeacon/);
  });

  it("lib/theme.ts is the one file allowed localStorage; a seeded call elsewhere fails", async () => {
    const eslint = new ESLint({ cwd: DASHBOARD });
    const source = 'export const read = () => localStorage.getItem("x");\n';
    const messages = async (path: string) =>
      (await eslint.lintText(source, { filePath: resolve(DASHBOARD, path) })).flatMap((r) =>
        r.messages.filter((m) => m.ruleId === "no-restricted-syntax"),
      );
    expect(await messages("lib/theme.ts")).toEqual([]);
    expect(await messages("lib/other.ts")).toHaveLength(1);
    expect(await messages("components/ThemeToggle.tsx")).toHaveLength(1);
    const arithmetic = "export const n = (a: number) => a + 1;\n";
    const result = await eslint.lintText(arithmetic, { filePath: resolve(DASHBOARD, "lib/theme.ts") });
    expect(result.flatMap((r) => r.messages).some((m) => m.ruleId === "no-restricted-syntax")).toBe(true);
  }, 60_000);
});
