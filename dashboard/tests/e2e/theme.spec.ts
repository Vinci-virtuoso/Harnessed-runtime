/**
 * FR-157 / decision 85: the theme choice is applied before first paint, switches with one press,
 * and never leaves the browser. Measured in the live browser, on the built export.
 *
 *   1. **First paint.** A stored "light" under an emulated dark system is `data-theme="light"` on
 *      `html` at `DOMContentLoaded`, before hydration: the static script is the only thing that ran.
 *   2. **Colours.** Each system scheme, with and without a stored choice, gives the expected
 *      computed `--color-bg`.
 *   3. **A press** issues 0 requests, stores the choice, and survives `localStorage` throwing.
 *   4. **No cross-fade**: no transition is running right after a press, with or without reduced motion.
 */
import { writeFileSync } from "node:fs";

import { expect, test, type Page } from "@playwright/test";

import { DASHBOARD_URL, evidencePath } from "./harness";

const TICKET = "251";
const KEY = "harnessed.theme";
const BG = { dark: "#0b0d10", light: "#f7f8fa" } as const;

const toggle = (page: Page) =>
  page.getByRole("navigation", { name: "Pages" }).getByRole("button", { name: /^Switch to (light|dark) theme$/ });

const bg = (page: Page) =>
  page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue("--color-bg").trim().toLowerCase());

test("a stored light choice is on html at DOMContentLoaded, under a dark system, before hydration", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "dark" });
  await page.addInitScript(
    ([key]) => {
      if (!window.localStorage.getItem(key)) window.localStorage.setItem(key, "light");
      document.addEventListener("DOMContentLoaded", () => {
        (window as unknown as { __atDcl: string | undefined }).__atDcl = document.documentElement.dataset.theme;
      });
    },
    [KEY],
  );
  await page.goto(`${DASHBOARD_URL}/`);
  const atDcl = await page.evaluate(() => (window as unknown as { __atDcl: string | undefined }).__atDcl);
  expect(atDcl, "data-theme at DOMContentLoaded").toBe("light");
  expect(await bg(page)).toBe(BG.light);
  await expect(toggle(page)).toHaveAttribute("title", "Switch to dark theme");
  await page.screenshot({ path: evidencePath(TICKET, "stored-light-under-dark.png") });

  await page.reload();
  expect(await page.evaluate(() => document.documentElement.dataset.theme)).toBe("light");
  writeFileSync(evidencePath(TICKET, "first-paint.txt"), `data-theme at DOMContentLoaded: ${atDcl}\n`, "utf8");
});

for (const stored of ["light", "dark"] as const) {
  test(`a stored ${stored} choice names the toggle's action at DOMContentLoaded, before hydration`, async ({ page }) => {
    await page.emulateMedia({ colorScheme: "dark" });
    await page.addInitScript(
      ([key, value]) => {
        window.localStorage.setItem(key, value);
        // Registered after the head script's own listener, so it reads what that script wrote.
        document.addEventListener("readystatechange", () => {
          if (document.readyState !== "interactive") return;
          document.addEventListener("DOMContentLoaded", () => {
            const button = document.querySelector("nav button[data-label-light]");
            (window as unknown as { __label: string | null }).__label = button?.getAttribute("aria-label") ?? null;
          });
        });
      },
      [KEY, stored],
    );
    await page.goto(`${DASHBOARD_URL}/`);
    const expected = stored === "light" ? "Switch to dark theme" : "Switch to light theme";
    expect(await page.evaluate(() => (window as unknown as { __label: string | null }).__label)).toBe(expected);
    await expect(toggle(page)).toHaveAttribute("aria-label", expected);
    await page.screenshot({ path: evidencePath("302", `label-stored-${stored}.png`) });
  });
}

for (const scheme of ["dark", "light"] as const) {
  test(`under a ${scheme} system: no stored choice follows the system, a stored choice wins`, async ({ browser }) => {
    for (const stored of [null, "dark", "light"] as const) {
      const context = await browser.newContext({ colorScheme: scheme });
      const page = await context.newPage();
      if (stored !== null) await page.addInitScript(([k, v]) => window.localStorage.setItem(k, v), [KEY, stored]);
      await page.goto(`${DASHBOARD_URL}/`);
      const expected = stored ?? scheme;
      expect(await page.evaluate(() => document.documentElement.dataset.theme ?? null), `${scheme}/${stored}`).toBe(stored);
      expect(await bg(page), `${scheme}/${stored}: --color-bg`).toBe(BG[expected]);
      await context.close();
    }
  });
}

test("a press switches the theme, stores it, makes 0 requests and has no transition", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "dark" });
  await page.goto(`${DASHBOARD_URL}/`);
  await expect(toggle(page)).toHaveAttribute("aria-label", "Switch to light theme");
  await expect(toggle(page)).toHaveAttribute("title", "Switch to light theme");
  await page.screenshot({ path: evidencePath(TICKET, "toggle-dark.png") });

  // Ticket 266: the log begins only once the page is quiet, so a first read still in flight on a
  // slower machine is not mistaken for the press. Every panel settled, then the network idle.
  await expect(page.locator("main h1").first()).toBeVisible();
  await expect.poll(() => page.locator('[aria-busy="true"]').count(), { timeout: 60_000 }).toBe(0);
  await page.waitForLoadState("networkidle");

  const log: string[] = [];
  page.on("request", (request) => log.push(`${request.method()} ${request.url()}`));
  await toggle(page).click();
  await page.waitForTimeout(300);
  expect(log, "requests a press made").toEqual([]);
  writeFileSync(evidencePath(TICKET, "press-request-log.txt"), `requests during a press: ${log.length}\n`, "utf8");

  expect(await page.evaluate(() => document.documentElement.dataset.theme)).toBe("light");
  expect(await page.evaluate((k) => window.localStorage.getItem(k), KEY)).toBe("light");
  expect(await bg(page)).toBe(BG.light);
  await expect(toggle(page)).toHaveAttribute("title", "Switch to dark theme");
  await page.screenshot({ path: evidencePath(TICKET, "toggle-light.png") });

  // FR-157 with FR-162: controls transition their own hover and press, but the swap itself leaves
  // no transition running anywhere, with or without reduced motion.
  for (const motion of ["no-preference", "reduce"] as const) {
    await page.emulateMedia({ reducedMotion: motion });
    await toggle(page).click();
    const running = await page.evaluate(
      () => document.getAnimations().filter((a) => a instanceof CSSTransition).length,
    );
    expect(running, `transitions running right after a press under ${motion}`).toBe(0);
    expect(await page.evaluate(() => document.documentElement.hasAttribute("data-theme-swap"))).toBe(false);
  }
  expect(await page.locator("[aria-live]:has-text('theme')").count(), "no announcement").toBe(0);
});

test("with localStorage throwing, the press still switches the theme and nothing errors", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.addInitScript(() => {
    const refuse = () => {
      throw new DOMException("blocked", "SecurityError");
    };
    Object.defineProperty(window, "localStorage", { get: refuse, configurable: true });
  });
  await page.emulateMedia({ colorScheme: "dark" });
  await page.goto(`${DASHBOARD_URL}/`);
  await toggle(page).click();
  expect(await page.evaluate(() => document.documentElement.dataset.theme)).toBe("light");
  expect(await bg(page)).toBe(BG.light);
  expect(errors).toEqual([]);
});
