/**
 * Ticket 253's walk (FR-154, UJ-26): the operator opens a task paused on one agent request,
 * reads "stub-agent asks to run a command" and the command itself, presses `Approve`, and the
 * card re-reads as approved. The request is a pending decision seeded under the e2e home through
 * the package's own writers (`global-setup.ts`); nothing here is stubbed.
 *
 * The network log is the proof of "one write": a `page.route("**\/*")` hook records every request,
 * and exactly one may be a `POST`, to `/control/v1/permissions/<the seeded id>`. 0 other writes
 * anywhere, 0 other origins.
 */
import { writeFileSync } from "node:fs";

import { expect, test } from "@playwright/test";

import {
  PERMISSION_AGENT,
  PERMISSION_DESCRIPTION,
  PERMISSION_SESSION,
  PERMISSION_TASK_TEXT,
  seededPermissionId,
} from "./global-setup";
import { DASHBOARD_URL, evidencePath, harnessHome, isWrite } from "./harness";

const TICKET = "253";
const ORIGIN = new URL(DASHBOARD_URL).origin;
const SETTLE_TIMEOUT_MS = 60_000;

type Logged = { method: string; url: string };

test("approving a pending request sends one POST and the card re-reads as approved", async ({ page }) => {
  test.setTimeout(120_000);
  const permissionId = seededPermissionId(harnessHome());
  const requests: Logged[] = [];
  await page.route("**/*", async (route) => {
    const request = route.request();
    requests.push({ method: request.method(), url: request.url() });
    if (new URL(request.url()).origin !== ORIGIN) return route.abort("blockedbyclient");
    return route.continue();
  });

  await page.goto(`${DASHBOARD_URL}/console?session=${encodeURIComponent(PERMISSION_SESSION)}`);

  const card = page.getByTestId("permission-card");
  await expect(card).toBeVisible({ timeout: SETTLE_TIMEOUT_MS });
  await expect(card.getByRole("heading", { level: 3 })).toHaveText(`${PERMISSION_AGENT} asks to run a command`);
  await expect(card.getByTestId("code-view").first()).toContainText(PERMISSION_DESCRIPTION);
  // The request lands on the exchange its recorded message_seq names, not beside the list (ticket 298).
  await expect(page.getByTestId("exchange").filter({ has: card })).toHaveCount(1);
  await expect(page.getByTestId("task-header")).toContainText(PERMISSION_TASK_TEXT);
  await expect(page.getByTestId("task-header")).toContainText("Needs you");
  await expect(page.getByRole("textbox", { name: "Message to the harness" })).toBeDisabled();
  await expect(page.getByText("Claude Code is waiting for your answer above")).toBeVisible();
  // Neither button took focus when the card arrived.
  await expect(card.getByRole("button", { name: "Approve" })).not.toBeFocused();
  await page.screenshot({ path: evidencePath(TICKET, "permission-pending.png"), fullPage: true });

  await card.getByRole("button", { name: "Approve" }).click();
  await expect(card).toHaveAttribute("data-state", "approved", { timeout: SETTLE_TIMEOUT_MS });
  await expect(card).toContainText("Approved by you at");
  await expect(card.getByRole("button", { name: "Approve" })).toHaveCount(0);
  await page.screenshot({ path: evidencePath(TICKET, "permission-approved.png"), fullPage: true });

  const writes = requests.filter((entry) => isWrite(entry.method, entry.url));
  expect(writes).toEqual([
    { method: "POST", url: `${DASHBOARD_URL}/control/v1/permissions/${permissionId}` },
  ]);
  expect(requests.filter((entry) => new URL(entry.url).origin !== ORIGIN)).toEqual([]);
  writeFileSync(
    evidencePath(TICKET, "request-log.txt"),
    `${requests.map((entry) => `${entry.method} ${new URL(entry.url).pathname}${new URL(entry.url).search}`).join("\n")}\n`,
    "utf8",
  );
});
