/**
 * FR-127 (ticket 189), in a real browser against the seeded process: a served `ui` output is
 * rendered by `UiPreview` - an `iframe` with `sandbox=""` exactly and `srcdoc` only, so a `<script>`
 * inside a seeded wireframe never runs - and its two controls behave as `design.md` describes:
 * "View source" switches the same output to `CodeView`, and "Open full size" opens a
 * focus-trapped modal.
 *
 * The seed (`global-setup.ts::seedOutputsSession`) is a real routed `harnessed run`, never a
 * hand-built fixture response: `FakeDecider` replays `src/harnessed/corpus/wire/jev/12659270cc4c`, which
 * routes to a `kind = "cli"` `stub-ui` entry that writes the seeded markup as a file its own
 * `outputs` glob picks up.
 */
import { expect, test } from "@playwright/test";

import { seededOutputsSessionId, UI_SCRIPT_FLAG } from "./global-setup";
import { DASHBOARD_URL, evidencePath, harnessHome } from "./harness";

const TICKET = "189";

test("a seeded UI output renders sandboxed, its script never runs, and its two controls work", async ({
  page,
}) => {
  const sessionId = seededOutputsSessionId(harnessHome());
  const response = await page.goto(`${DASHBOARD_URL}/console?session=${sessionId}`);
  expect(response?.status()).toBe(200);

  const exchange = page.getByTestId("exchange").first();
  await expect(exchange).toBeVisible();
  await exchange.getByRole("button", { name: "Steps" }).click();

  const uiOutput = exchange.locator('[data-testid="output-view"][data-kind="ui"]');
  await expect(uiOutput).toHaveCount(1);
  const preview = uiOutput.getByTestId("ui-preview");
  await expect(preview).toBeVisible();

  // 0 endpoint strings, 0 `src`: the markup is fetched off `lib/outputs.ts` and handed to
  // `srcdoc`, never linked - `D-10`.
  const iframe = preview.getByTestId("ui-preview-frame");
  await expect(iframe).toHaveAttribute("sandbox", "");
  expect(await iframe.getAttribute("src")).toBeNull();
  expect(await iframe.getAttribute("srcdoc")).toContain("seeded-wireframe");

  // 0 scripts ran: the sandboxed iframe carries no `allow-scripts`, so the seeded `<script>`
  // never sets the flag it would on an unsandboxed page - the top window's own global stays
  // `undefined` through every step below, not only this first check.
  async function scriptFlag(): Promise<unknown> {
    return page.evaluate((name) => (window as unknown as Record<string, unknown>)[name], UI_SCRIPT_FLAG);
  }
  expect(await scriptFlag()).toBeUndefined();

  await page.screenshot({ path: evidencePath(TICKET, "ui-output-sandboxed.png"), animations: "disabled" });

  // "View source" switches the same output to `CodeView`, the raw markup verbatim - the script
  // tag is visible as text, never executed.
  await preview.getByRole("button", { name: "View source" }).click();
  const codeView = preview.getByTestId("code-view");
  await expect(codeView).toBeVisible();
  expect(await codeView.textContent()).toContain("<script>");
  expect(await scriptFlag()).toBeUndefined();
  await page.screenshot({ path: evidencePath(TICKET, "ui-output-source.png"), animations: "disabled" });
  await preview.getByRole("button", { name: "View preview" }).click();

  // "Open full size" opens a focus-trapped modal: focus starts on its one control, Tab and
  // Shift+Tab both keep it there (the trap), and Escape closes it and returns focus to the
  // opener.
  const opener = preview.getByRole("button", { name: "Open full size" });
  await opener.click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  const close = dialog.getByRole("button", { name: "Close" });
  await expect(close).toBeFocused();

  await page.keyboard.press("Tab");
  await expect(close).toBeFocused();
  await page.keyboard.press("Shift+Tab");
  await expect(close).toBeFocused();

  const modalFrame = dialog.getByTestId("ui-preview-modal-frame");
  await expect(modalFrame).toHaveAttribute("sandbox", "");
  expect(await modalFrame.getAttribute("srcdoc")).toContain("seeded-wireframe");
  expect(await scriptFlag()).toBeUndefined();
  await page.screenshot({ path: evidencePath(TICKET, "ui-output-full-size.png"), animations: "disabled" });

  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(opener).toBeFocused();
});
