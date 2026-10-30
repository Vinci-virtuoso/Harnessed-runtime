/**
 * The dashboard's third-party notices (ticket 336, decision 108). Every package the lockfile does
 * not mark `dev`, minus the reviewed `BUILD_ONLY` set, is listed in `THIRD_PARTY_NOTICES.md` at its
 * locked version, and the built export holds no file of a `BUILD_ONLY` package. `BUILD_ONLY` is read
 * from `scripts/notices.py`, the one place it is written.
 */
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { join, resolve } from "node:path";
import { describe, expect, it } from "vitest";

const DASHBOARD = resolve(import.meta.dirname, "../..");
const ROOT = resolve(DASHBOARD, "..");
const LOCK = JSON.parse(readFileSync(resolve(DASHBOARD, "package-lock.json"), "utf8")) as {
  packages: Record<string, { version: string; dev?: boolean }>;
};
const NOTICES = readFileSync(resolve(ROOT, "THIRD_PARTY_NOTICES.md"), "utf8");
const SCRIPT = readFileSync(resolve(ROOT, "scripts/notices.py"), "utf8");
const OUT = resolve(DASHBOARD, "out");

/** `BUILD_ONLY = ("@img/*", "sharp", ...)` in the script, as its patterns. */
function buildOnly(script: string): string[] {
  const tuple = /^BUILD_ONLY = \(([^)]*)\)/m.exec(script);
  if (!tuple) throw new Error("scripts/notices.py declares no BUILD_ONLY");
  return [...tuple[1].matchAll(/"([^"]+)"/g)].map((m) => m[1]);
}

function matches(name: string, pattern: string): boolean {
  return pattern.endsWith("*") ? name.startsWith(pattern.slice(0, -1)) : name === pattern;
}

/** The lockfile's non-dev entries minus BUILD_ONLY (sharp covers what is nested under it). */
function bundled(lock: typeof LOCK, patterns: string[]): string[] {
  return Object.entries(lock.packages)
    .filter(([path, entry]) => path !== "" && !entry.dev && !path.startsWith("node_modules/sharp/"))
    .map(([path, entry]) => [path.slice(path.lastIndexOf("node_modules/") + "node_modules/".length), entry.version])
    .filter(([name]) => !patterns.some((pattern) => matches(name, pattern)))
    .map(([name, version]) => `${name}@${version}`);
}

function listed(notices: string): string[] {
  return [...notices.matchAll(/^\| (\S+) \| (\S+) \| .+? \| dashboard \|$/gm)].map((m) => `${m[1]}@${m[2]}`);
}

function files(directory: string): string[] {
  return readdirSync(directory).flatMap((name) => {
    const path = join(directory, name);
    return statSync(path).isDirectory() ? files(path) : [path];
  });
}

const PATTERNS = buildOnly(SCRIPT);

describe("third-party notices", () => {
  it("reads the reviewed BUILD_ONLY set from the script", () => {
    expect(PATTERNS).toEqual(["@img/*", "sharp", "@next/swc-*", "caniuse-lite"]);
  });

  it("lists exactly the lockfile's non-dev set minus BUILD_ONLY, at the locked versions", () => {
    expect(listed(NOTICES)).toEqual(bundled(LOCK, PATTERNS));
  });

  it("fails on a seeded lockfile entry the notices do not list", () => {
    const seeded = { packages: { ...LOCK.packages, "node_modules/left-pad": { version: "1.3.0" } } };
    expect(listed(NOTICES)).not.toEqual(bundled(seeded, PATTERNS));
  });

  it.skipIf(!existsSync(OUT))("leaves no file of a BUILD_ONLY package in the built export", () => {
    const markers = PATTERNS.map((p) => p.replace("*", ""));
    const offenders = files(OUT).filter((path) => {
      const text = readFileSync(path, "latin1");
      return markers.some((marker) => path.includes(marker) || text.includes(`node_modules/${marker}`));
    });
    expect(offenders).toEqual([]);
  });
});
