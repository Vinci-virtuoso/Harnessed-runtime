/**
 * FR-162 in the live browser: every control answers the pointer within `motion-micro`, a press
 * moves a button 1px, a disabled button does neither, and under reduced motion each control still
 * reaches its hover state with a 0s duration (design.md, "Controls respond"). Computed styles are
 * read from the built export; no sentence the dashboard prints about itself is trusted.
 */
import { readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

import { expect, test, type Locator, type Page } from "@playwright/test";

import { DASHBOARD_ROOT, DASHBOARD_URL, evidencePath } from "./harness";

const TICKET = "260";
const MICRO = /^0\.15s(, 0\.15s)*$/;
const ZERO = /^0s(, 0s)*$/;

/** One recorded `needs_human` listing, so the Overview has a row to hover without a seeded pause. */
const NEEDS_YOU = readFileSync(
  resolve(
    DASHBOARD_ROOT,
    "tests/fixtures/gateway/tasked.tasks.needs-human.body",
  ),
  "utf8",
);

async function style(locator: Locator, property: string): Promise<string> {
  return locator.evaluate(
    (el, name) => getComputedStyle(el).getPropertyValue(name),
    property,
  );
}

async function withNeedsYou(page: Page): Promise<void> {
  await page.route("**/v1/tasks?state=needs_human", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: NEEDS_YOU,
    }),
  );
}

type Control = {
  name: string;
  path: string;
  find: (page: Page) => Locator;
  /** The property the table says hover changes, and what it must stop being. */
  property: string;
  /** Prepares the page (the console's Send needs text before it is enabled). */
  ready?: (page: Page) => Promise<void>;
  press?: boolean;
};

const CONTROLS: Control[] = [
  {
    name: "a nav item",
    path: "/",
    find: (page) =>
      page
        .getByRole("navigation", { name: "Pages" })
        .getByRole("link", { name: "Traces" }),
    property: "color",
  },
  {
    name: "the shell's Refresh",
    path: "/",
    find: (page) => page.getByRole("button", { name: "Refresh", exact: true }),
    property: "border-top-color",
    press: true,
  },
  {
    name: "the console's Send",
    path: "/console",
    find: (page) => page.getByRole("button", { name: "Send", exact: true }),
    property: "opacity",
    ready: async (page) => {
      await page.getByLabel("Message to the harness").fill("hello");
    },
    press: true,
  },
  {
    name: "a NeedsYouList row",
    path: "/",
    find: (page) => page.getByTestId("needs-you-list").locator("li").first(),
    property: "background-color",
  },
  {
    name: "a Value button",
    path: "/",
    find: (page) => page.locator("button[data-figure]").first(),
    property: "background-color",
  },
];

async function open(page: Page, control: Control): Promise<Locator> {
  await withNeedsYou(page);
  await page.goto(`${DASHBOARD_URL}${control.path}`);
  await control.ready?.(page);
  const target = control.find(page);
  await expect(target).toBeVisible({ timeout: 30_000 });
  return target;
}

for (const control of CONTROLS) {
  test(`${control.name} changes on hover over 0.15s`, async ({ page }) => {
    const target = await open(page, control);
    const rest = await style(target, control.property);
    expect(await style(target, "transition-duration")).toMatch(MICRO);
    await target.hover();
    await page.waitForTimeout(400);
    expect(
      await style(target, control.property),
      `${control.property} on hover`,
    ).not.toBe(rest);
    await page.screenshot({
      path: evidencePath(
        TICKET,
        `${control.name.replace(/\W+/g, "-")}-hover.png`,
      ),
    });
  });

  test(`${control.name} still reaches its hover state, instantly, under reduced motion`, async ({
    page,
  }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    const target = await open(page, control);
    const rest = await style(target, control.property);
    expect(await style(target, "transition-duration")).toMatch(ZERO);
    await target.hover();
    expect(
      await style(target, control.property),
      `${control.property} on hover`,
    ).not.toBe(rest);
    expect(await style(target, "transition-duration")).toMatch(ZERO);
  });

  if (control.press) {
    test(`${control.name} moves 1px when pressed`, async ({ page }) => {
      const target = await open(page, control);
      expect(await style(target, "transform")).toBe("none");
      await target.hover();
      await page.mouse.down();
      await page.waitForTimeout(400);
      expect(await style(target, "transform")).toBe("matrix(1, 0, 0, 1, 0, 1)");
      // Release off the control so the press is not a click: the walk sends nothing.
      await page.mouse.move(2, 2);
      await page.mouse.up();
    });
  }
}

test("a disabled Send changes with neither hover nor press", async ({
  page,
}) => {
  await page.goto(`${DASHBOARD_URL}/console`);
  const send = page.getByRole("button", { name: "Send", exact: true });
  await expect(send).toBeDisabled({ timeout: 30_000 });
  const rest = {
    opacity: await style(send, "opacity"),
    transform: await style(send, "transform"),
  };
  await send.hover({ force: true });
  await page.mouse.down();
  await page.waitForTimeout(400);
  const held = {
    opacity: await style(send, "opacity"),
    transform: await style(send, "transform"),
  };
  await page.mouse.up();
  expect(held).toEqual(rest);
  writeFileSync(
    evidencePath(TICKET, "disabled.txt"),
    JSON.stringify({ rest, held }) + "\n",
    "utf8",
  );
});
