/**
 * Every `page` capture in the launch video's manifest, in its theme, saved as the file it names
 * (ticket 343, FR-188). A capture is the built dashboard as it renders a real home: nothing is
 * stubbed, routed or edited. A capture it cannot take fails by its id.
 */
import { spawnSync } from "node:child_process";
import { mkdirSync } from "node:fs";
import { join, resolve } from "node:path";
import { expect, test } from "@playwright/test";
import { pendingSession } from "./pending-session";

const REPO_ROOT = resolve(import.meta.dirname, "../../..");
const VIDEO = join(REPO_ROOT, ".spec", "launch", "video");
const OUT = process.env.HARNESSED_CAPTURE_OUT ?? join(VIDEO, "captures");

type Capture = { id: string; kind: "cli" | "page"; page?: string; theme: "dark" | "light"; home: string; file: string };

/** The manifest, parsed by the same YAML reader the Python tests use. */
function manifest(): Capture[] {
  const read = spawnSync(
    "uv",
    ["run", "python", "-c", "import json, sys, yaml; print(json.dumps(yaml.safe_load(open(sys.argv[1]))))", join(VIDEO, "captures", "manifest.yaml")],
    { cwd: REPO_ROOT, encoding: "utf8", shell: false },
  );
  if (read.status !== 0) throw new Error(`the manifest did not parse: ${read.stderr}`);
  return (JSON.parse(read.stdout) as { captures: Capture[] }).captures;
}

function served(home: string): string | undefined {
  return process.env[`HARNESSED_CAPTURE_URL_${home.toUpperCase().replace("-", "_")}`];
}

mkdirSync(OUT, { recursive: true });

for (const capture of manifest().filter((c) => c.kind === "page")) {
  test(capture.id, async ({ page }) => {
    const base = served(capture.home);
    if (!base) {
      throw new Error(`cannot take ${capture.id}: no ${capture.home} home was given (see playwright.config.ts)`);
    }
    await page.addInitScript((theme) => window.localStorage.setItem("harnessed.theme", theme), capture.theme);
    let route = capture.page;
    if (capture.id === "page-console-permission") {
      const response = await page.request.get(`${base}/v1/permissions?state=pending`);
      if (!response.ok()) throw new Error(`${capture.id}: pending permissions unavailable`);
      const session = pendingSession(await response.json());
      route = `/console?session=${encodeURIComponent(session)}`;
    }
    await page.goto(`${base}${route}`, { waitUntil: "domcontentloaded" });
    await expect(page.locator("html")).toHaveAttribute("data-theme", capture.theme);
    await expect(page.locator("main")).toBeVisible();
    if (capture.id === "page-console-permission") {
      // The scene shows a real request waiting for its answer; without one there is nothing to stage.
      await expect(page.getByRole("button", { name: "Approve" }).first()).toBeVisible();
    }
    if (capture.id === "page-overview-totals") {
      const tasks = page.getByRole("heading", { name: "Tasks", exact: true });
      await expect(page.getByText("Completed unattended", { exact: true })).toBeVisible();
      await tasks.scrollIntoViewIfNeeded();
      await expect(tasks).toBeInViewport();
    }
    await page.screenshot({ path: join(OUT, capture.file) });
  });
}
