#!/usr/bin/env node
/**
 * The export stage (FR-63, decision 44): stage `dashboard/out/` into the Python package and
 * write the staleness stamp beside it.
 *
 * `next build` with `output: "export"` writes `out/`; this copies it, whole, to
 * `src/harnessed/dashboard_static/` and writes `.stamp`, a sha256 over the sorted relative paths
 * and contents of `dashboard/{app,components,lib,public}/**`, `package-lock.json`,
 * `next.config.ts` and `tsconfig.json`. `tests/test_dashboard_export.py` recomputes the same
 * digest and fails naming the first file that differs, which is what makes a stale export loud.
 *
 * This runs from the dashboard harness's `export` stage, never from `uv build`: a build backend
 * that shelled out to node would make `uv sync` need node, the one thing FR-63 exists to prevent.
 *
 * Usage: node scripts/stage-export.mjs [dashboardDir] [outDir] [targetDir]
 * The arguments exist so the test can run it over a synthetic tree; the defaults are the real one.
 */
import { createHash } from "node:crypto";
import { cpSync, existsSync, mkdirSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

/** What the stamp covers, relative to the dashboard root. The Python test names the same set. */
const STAMPED_TREES = ["app", "components", "lib", "public"];
const STAMPED_FILES = ["package-lock.json", "next.config.ts", "tsconfig.json"];
const STAMP_NAME = ".stamp";

const BUILD_COMMAND = "npm --prefix dashboard run build";

const here = dirname(fileURLToPath(import.meta.url));
const [dashboardArg, outArg, targetArg] = process.argv.slice(2);
const dashboard = resolve(dashboardArg ?? join(here, ".."));
const out = resolve(outArg ?? join(dashboard, "out"));
const target = resolve(targetArg ?? join(dashboard, "..", "src", "harnessed", "dashboard_static"));

/** Every file under `root`, as relative posix paths. */
function walk(root, prefix = "") {
  const found = [];
  for (const entry of readdirSync(root, { withFileTypes: true })) {
    const relative = prefix === "" ? entry.name : `${prefix}/${entry.name}`;
    if (entry.isDirectory()) found.push(...walk(join(root, entry.name), relative));
    else if (entry.isFile()) found.push(relative);
  }
  return found;
}

/** The stamped files, sorted by relative path: the order the digest and the message both use. */
function stampedFiles() {
  const found = [];
  for (const tree of STAMPED_TREES) {
    const base = join(dashboard, tree);
    if (existsSync(base)) found.push(...walk(base, tree));
  }
  for (const name of STAMPED_FILES) {
    if (existsSync(join(dashboard, name))) found.push(name);
  }
  return found.sort();
}

function stamp() {
  const overall = createHash("sha256");
  const lines = [];
  for (const relative of stampedFiles()) {
    const data = readFileSync(join(dashboard, relative));
    overall.update(relative, "utf8");
    overall.update("\0");
    overall.update(String(data.length), "ascii");
    overall.update("\0");
    overall.update(data);
    lines.push(`${createHash("sha256").update(data).digest("hex")}  ${relative}`);
  }
  return [`sha256:${overall.digest("hex")}`, ...lines].join("\n") + "\n";
}

if (!existsSync(join(out, "index.html"))) {
  process.stderr.write(`no export at ${out}; run ${BUILD_COMMAND} first\n`);
  process.exit(1);
}

rmSync(target, { recursive: true, force: true });
mkdirSync(dirname(target), { recursive: true });
cpSync(out, target, { recursive: true });
writeFileSync(join(target, STAMP_NAME), stamp(), "utf8");
process.stdout.write(`staged ${out} -> ${target} (${STAMP_NAME} written)\n`);
