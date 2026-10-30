/**
 * FR-158 / decision 84: every page is glass in both themes and solid when asked, and reads as
 * well as solid. Measured in the live browser, on the built export, never from a sentence.
 *
 *   1. **Solid.** Under each trigger the stylesheet answers - `prefers-reduced-transparency`,
 *      `prefers-contrast: more` (both emulated), and `data-surface="solid"` on the page root (what
 *      `SOLID_PAGES` renders) - computed `backdrop-filter` is `none` on every element, each
 *      surface's background is its solid token's computed value, the ground is `color-bg`, and
 *      every surface's bounding box is the one glass mode laid out. (`@supports not` cannot be
 *      emulated; `glass-tokens.test.ts` proves its block is the same text.)
 *   2. **Glass.** No `background-image` gradient other than the field, the primary action and the
 *      functional `unknown` hatch; every text colour is one of the v5 tokens (G-1); and on Traces
 *      with 100 seeded rows `backdrop-filter` sits on no row, figure or animating element and on
 *      no more elements than at 20 rows (G-5).
 */
import { writeFileSync } from "node:fs";

import { expect, test, type Browser, type CDPSession, type Page } from "@playwright/test";

import { DASHBOARD_URL, evidencePath } from "./harness";

const TICKET = "250";

/** Every page family of `app/`; the seeded-id variants add nothing to the surface layer. */
const PAGES = ["/", "/traces?limit=100", "/console", "/evals", "/improve", "/memory", "/models"];

type Scheme = "dark" | "light";
type Trigger = "reduced-transparency" | "contrast-more" | "data-surface";
const TRIGGERS: readonly Trigger[] = ["reduced-transparency", "contrast-more", "data-surface"];

/** The v5 text, figure and status colours per theme (design.md "Color tokens"; light `color-pass`
 * is the one value the 2026-10-04 UX ruling moved). Computed `color` must be one of these. */
const TEXT_COLOURS: Record<Scheme, string[]> = {
  dark: ["#E6E9ED", "#A9B1BC", "#7D8693", "#4F5762", "#8E99FA", "#4DBF8A", "#E8746D", "#D9A94E"],
  light: ["#0F1217", "#3C4450", "#67707D", "#9AA2AD", "#4F5BD5", "#1B7D4E", "#BF3A32", "#935E00"],
};

function rgb(hex: string): string {
  const n = Number.parseInt(hex.slice(1), 16);
  return `rgb(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255})`;
}

type Surface = { path: string; box: [number, number, number, number]; background: string; filter: string };

/** Everything the checks need from one rendered page, read in one pass. */
async function inspect(page: Page) {
  return page.evaluate(() => {
    const pathOf = (el: Element): string => {
      const parts: string[] = [];
      for (let node: Element | null = el; node && node !== document.body; node = node.parentElement) {
        const parent: Element | null = node.parentElement;
        const index = parent ? Array.prototype.indexOf.call(parent.children, node) : 0;
        parts.unshift(`${node.tagName.toLowerCase()}:${index}`);
      }
      return parts.join(">");
    };
    const filterOf = (el: Element) => {
      const cs = getComputedStyle(el);
      return cs.backdropFilter && cs.backdropFilter !== "none"
        ? cs.backdropFilter
        : ((cs as unknown as Record<string, string>).webkitBackdropFilter ?? "none");
    };
    const surfaces: Surface[] = [];
    const gradients: string[] = [];
    const colours = new Set<string>();
    const badFilter: string[] = [];
    for (const el of Array.from(document.body.querySelectorAll("*"))) {
      const cs = getComputedStyle(el);
      const filter = filterOf(el);
      const r = el.getBoundingClientRect();
      if (filter !== "none") {
        surfaces.push({
          path: pathOf(el),
          box: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
          background: cs.backgroundColor,
          filter,
        });
        const tag = el.tagName.toLowerCase();
        if (
          tag === "tr" || tag === "li" || tag === "td" || tag === "th" ||
          el.matches('[role="row"], [data-figure], [data-testid*="row"]') ||
          el.getAnimations().length > 0
        ) {
          badFilter.push(`${tag} ${pathOf(el)}`);
        }
      }
      if (cs.backgroundImage.includes("gradient")) gradients.push(`${pathOf(el)} ${cs.backgroundImage}`);
      const hasText = Array.from(el.childNodes).some((n) => n.nodeType === 3 && (n.textContent ?? "").trim() !== "");
      if (hasText) colours.add(cs.color);
    }
    const before = getComputedStyle(document.body, "::before");
    return {
      surfaces,
      gradients,
      colours: [...colours],
      badFilter,
      ground: getComputedStyle(document.body).backgroundColor,
      field: { image: before.backgroundImage, position: before.position, attachment: before.backgroundAttachment },
    };
  }) as Promise<{
    surfaces: Surface[];
    gradients: string[];
    colours: string[];
    badFilter: string[];
    ground: string;
    field: { image: string; position: string; attachment: string };
  }>;
}

/** The solid token a surface's background must equal, as computed rgb, per theme. */
async function solidTokens(page: Page) {
  return page.evaluate(() => {
    const probe = document.createElement("div");
    document.body.appendChild(probe);
    const resolve = (name: string) => {
      probe.style.backgroundColor = `var(${name})`;
      return getComputedStyle(probe).backgroundColor;
    };
    const out = {
      surface: resolve("--color-surface"),
      raised: resolve("--color-surface-raised"),
      sunken: resolve("--color-surface-sunken"),
      bg: resolve("--color-bg"),
    };
    probe.remove();
    return out;
  });
}

async function emulate(page: Page, scheme: Scheme, trigger: Trigger | null): Promise<void> {
  const cdp: CDPSession = await page.context().newCDPSession(page);
  const features: { name: string; value: string }[] = [{ name: "prefers-color-scheme", value: scheme }];
  if (trigger === "reduced-transparency") features.push({ name: "prefers-reduced-transparency", value: "reduce" });
  if (trigger === "contrast-more") features.push({ name: "prefers-contrast", value: "more" });
  await cdp.send("Emulation.setEmulatedMedia", { features });
}

async function load(page: Page, at: string): Promise<void> {
  await page.goto(new URL(at, DASHBOARD_URL).toString(), { waitUntil: "load" });
  await page.waitForLoadState("networkidle");
  await page.locator("main").first().waitFor();
  await page.waitForTimeout(400);
}

async function fresh(browser: Browser, scheme: Scheme) {
  const context = await browser.newContext({ colorScheme: scheme, viewport: { width: 1280, height: 900 } });
  return { context, page: await context.newPage() };
}

const log: string[] = [];

for (const scheme of ["dark", "light"] as const) {
  test(`glass: ${scheme}: surfaces blur, nothing else changes, rows never blur`, async ({ browser }) => {
    test.setTimeout(300_000);
    const { context, page } = await fresh(browser, scheme);
    try {
      await emulate(page, scheme, null);
      const allowed = new Set(TEXT_COLOURS[scheme].map(rgb));
      let counts20 = 0;
      for (const at of PAGES) {
        await load(page, at);
        const seen = await inspect(page);
        expect(seen.surfaces.length, `${at} has glass surfaces`).toBeGreaterThan(0);
        expect(seen.badFilter, `${at}: backdrop-filter on a row, figure or animating element`).toEqual([]);
        expect(seen.field.position, `${at} field`).toBe("fixed");
        expect(seen.field.attachment, `${at} field`).not.toContain("fixed");
        expect(seen.field.image, `${at} field`).toContain("radial-gradient");
        const stray = seen.gradients.filter(
          (g) => !g.includes("repeating-linear-gradient") && !/linear-gradient\(180deg/.test(g),
        );
        expect(stray, `${at}: gradients other than the field, the primary action and the hatch`).toEqual([]);
        const off = seen.colours.filter((c) => !allowed.has(c) && !/^rgb\(255, 255, 255\)$|^rgb\(11, 13, 16\)$|^rgb\(7, 9, 12\)$/.test(c));
        expect(off, `${at}: text colours that are not v5 tokens`).toEqual([]);
        log.push(`${scheme} ${at}: ${seen.surfaces.length} blurred surfaces, ${seen.colours.length} text colours`);
        if (at === "/traces?limit=100") {
          const rows = await page.locator("table tbody tr, [role=row], ul li").count();
          expect(rows, "Traces renders the seeded rows").toBeGreaterThanOrEqual(100);
          await load(page, "/traces?limit=20");
          counts20 = (await inspect(page)).surfaces.length;
          expect(seen.surfaces.length, "blurred elements do not grow with rows (G-5)").toBe(counts20);
        }
        await page.screenshot({ path: evidencePath(TICKET, `glass-${scheme}-${at.replace(/\W+/g, "_")}.png`) });
      }
    } finally {
      await context.close();
      writeFileSync(evidencePath(TICKET, "glass-e2e.txt"), [...log, ""].join("\n"), "utf8");
    }
  });

  test(`schedule: ${scheme}: "Not installed" reads in color-warn through Value (ticket 272)`, async ({ browser }) => {
    const { context, page } = await fresh(browser, scheme);
    try {
      await emulate(page, scheme, null);
      await page.route("**/v1/schedules", (route) =>
        route.fulfill({
          contentType: "application/json",
          body: JSON.stringify({
            schedules: [
              {
                installed: "false",
                last_run_at: null,
                next_due_at: "2026-10-06T16:34:00+00:00",
                rule_kind: "every",
                rule_value: "15m",
                rule_words: "Every 15 min",
                schedule_id: "sch_272",
                where: "task_scheduler",
              },
            ],
          }),
        }),
      );
      await load(page, "/traces");
      const word = page.getByTestId("schedule-row").getByText("Not installed");
      await expect(word).toBeVisible();
      const colour = await word.evaluate((el) => getComputedStyle(el).color);
      expect(colour).toBe(rgb(TEXT_COLOURS[scheme][7]));
      await page.screenshot({ path: evidencePath("272", `schedule-${scheme}.png`) });
    } finally {
      await context.close();
    }
  });

  for (const trigger of TRIGGERS) {
    test(`solid: ${scheme}: ${trigger} drops every blur and keeps every box`, async ({ browser }) => {
      test.setTimeout(300_000);
      const { context, page } = await fresh(browser, scheme);
      try {
        for (const at of PAGES) {
          await emulate(page, scheme, null);
          await load(page, at);
          const glass = await inspect(page);

          await emulate(page, scheme, trigger);
          if (trigger === "data-surface") {
            await page.evaluate(() => document.querySelector("[data-surface], main")?.closest("div")?.setAttribute("data-surface", "solid"));
          } else {
            await load(page, at);
          }
          await page.waitForTimeout(200);
          const solid = await inspect(page);
          const tokens = await solidTokens(page);

          expect(solid.surfaces, `${at} [${trigger}]: elements still blurred`).toEqual([]);
          expect(solid.ground, `${at} [${trigger}]: ground`).toBe(tokens.bg);
          // The shell's root takes the attribute on a real page (`SOLID_PAGES`); its `div` is the first
          // element carrying `data-surface`, so a bare `main` fallback above never matters here.
          const solidBackgrounds = new Set([tokens.surface, tokens.raised, tokens.sunken, tokens.bg]);
          const geometry = await page.evaluate(() =>
            Array.from(document.body.querySelectorAll("*"))
              .filter((el) => el.getBoundingClientRect().width > 0)
              .map((el) => {
                const r = el.getBoundingClientRect();
                return [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)];
              }),
          );
          expect(geometry.length).toBeGreaterThan(0);
          for (const surface of glass.surfaces) {
            const now = await page.evaluate(
              ([path]) => {
                const walk = path.split(">").reduce<Element | null>(
                  (node, part) => {
                    if (!node) return null;
                    const index = Number(part.split(":")[1]);
                    return node.children[index] ?? null;
                  },
                  document.body,
                );
                if (!walk) return null;
                const r = walk.getBoundingClientRect();
                return {
                  box: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
                  background: getComputedStyle(walk).backgroundColor,
                };
              },
              [surface.path],
            );
            expect(now, `${at} [${trigger}]: ${surface.path} is gone`).not.toBeNull();
            expect(now!.box, `${at} [${trigger}]: ${surface.path} box`).toEqual(surface.box);
            expect(solidBackgrounds.has(now!.background), `${at} [${trigger}]: ${surface.path} background ${now!.background}`).toBe(true);
          }
        }
        await page.screenshot({ path: evidencePath(TICKET, `solid-${scheme}-${trigger}.png`) });
      } finally {
        await context.close();
      }
    });
  }
}
