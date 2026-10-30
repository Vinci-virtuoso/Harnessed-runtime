/**
 * The e2e harness's fixed port and paths, shared by `playwright.config.ts`, `global-setup.ts`
 * and the specs. One process, `uv run harnessed dashboard`, serves the UI at `/` and the read
 * gateway at `/v1` on one loopback port that is never the operator's 8756 (decision 30, FR-66).
 */
import { spawnSync } from "node:child_process";
import { mkdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";

export const REPO_ROOT = resolve(import.meta.dirname, "../../..");
export const DASHBOARD_ROOT = resolve(REPO_ROOT, "dashboard");

/** The seeded `harnessed dashboard`: the UI and, mounted beside it, the gateway. */
export const DASHBOARD_PORT = 18756;

export const DASHBOARD_URL = `http://127.0.0.1:${DASHBOARD_PORT}`;
/** The read gateway, mounted in the same process at the relative `/v1` (decision 43). */
export const GATEWAY_URL = `${DASHBOARD_URL}/v1`;

/**
 * Where the run's temporary `HARNESSED_HOME` lives. The config creates it once in the runner
 * process and every worker inherits the variable, so all of them agree on one directory.
 */
export const HOME_VARIABLE = "HARNESSED_DASHBOARD_E2E_HOME";

/**
 * The fake provider's test-only latency knob (ticket 119, FR-53), and the delay this harness
 * sets it to.
 *
 * `FakeProvider` reads a committed fixture off disk and returns, so an unslowed
 * `harnessed eval run smoke --fresh` publishes all 31 of its events inside about 24 ms, roughly
 * 1.6 s into the process (ticket 112 measured it into `evidence/112/stream-e2e.txt`). A session
 * does not exist before its first event, so at that speed there is no instant at which it is both
 * discoverable and still unfinished: the page replays every row, the cursor is already terminal,
 * and the stream delivers zero frames. A walk that means to watch a run live needs the publisher
 * to take its time.
 *
 * 1500 ms is chosen against the smoke suite's shape rather than picked round. The suite is five
 * cases and one of them takes two iterations, so the run makes at least six completions and the
 * publish window becomes ~9 s - wide enough that finding the session, clicking through to it and
 * subscribing all land while events are still owed, and narrow enough that two specs at one
 * run each cost the e2e stage about 20 s. It is the environment and only the environment: the
 * variable is in no `.env`, in no `Settings` and on no command's flags, so it cannot reach a real
 * provider by accident.
 */
export const FAKE_LATENCY_VARIABLE = "HARNESSED_FAKE_LATENCY_MS";
export const FAKE_LATENCY_MS = 1500;

/**
 * The working directory the `harnessed dashboard` process runs in: a sibling of its home, never
 * the checkout. The control app writes `.env` into the working directory (FR-88, MUST rule 11),
 * so the models walk's saved key lands here and never in the operator's own `.env` (ticket 149).
 */
export function serverDirectory(home: string): string {
  return join(dirname(home), "cwd");
}

/**
 * A stand-in for a provider's OpenAI-compatible endpoint, which the models walk's one validation
 * call reaches instead of the vendor (FR-88). The dashboard process is told about it through
 * `HARNESSED_BASE_URL`, which only a keyed provider reads; the fake provider every run uses is
 * built offline and never asks it anything. It is the server's call, not the browser's, so the
 * browser's "0 other origins" is untouched by it.
 */
export const STUB_PORT = 18760;
export const STUB_BASE_URL = `http://127.0.0.1:${STUB_PORT}/v1`;

/** `evidence/<ticket>/<name>`, with the directory made. `evidence/` is gitignored. */
export function evidencePath(ticket: string, name: string): string {
  const directory = resolve(REPO_ROOT, "evidence", ticket);
  mkdirSync(directory, { recursive: true });
  return resolve(directory, name);
}

// ---- The ledger, read and written the way the package does it -----------------------------------

/** The run's temporary `HARNESSED_HOME`, which the dashboard process serves. */
export function harnessHome(): string {
  const directory = process.env[HOME_VARIABLE];
  if (!directory) throw new Error(`${HOME_VARIABLE} is not set; playwright.config.ts sets it`);
  return directory;
}

type Done = { status: number | null; stdout: string; stderr: string };

/** One `uv run` under `home` and the fake provider, from the checkout. */
function uv(home: string, argv: string[]): Done {
  const done = spawnSync("uv", ["run", ...argv], {
    cwd: REPO_ROOT,
    env: { ...process.env, HARNESSED_HOME: home, HARNESSED_PROVIDER: "fake" },
    encoding: "utf8",
    shell: false,
  });
  return { status: done.status, stdout: done.stdout ?? "", stderr: done.stderr ?? "" };
}

/** One CLI call under `home` (the harness's own by default), as an operator makes it. */
export function cli(argv: string[], home: string = harnessHome()): Done {
  return uv(home, ["harnessed", ...argv]);
}

/** `PRAGMA user_version` and every table's row count. */
export type Ledger = { user_version: number; counts: Record<string, number> };

/**
 * `PRAGMA user_version` and the row count of every table, read through the package's read-only
 * connection (`db.connect_readonly`, MUST rule 13). The tables are enumerated from `sqlite_master`,
 * not listed here, so a table a migration adds is counted without this file being edited.
 *
 * `migrate` opens with `db.connect()` instead, which creates and migrates a home that has no
 * database yet: the console walk's CLI path starts from one, and its baseline is the schema alone.
 */
export function ledger(home: string = harnessHome(), migrate = false): Ledger {
  const open = migrate ? "db.connect()" : "db.connect_readonly(db.database_path())";
  const script = [
    `import json`,
    `from harnessed.memory import db`,
    `conn = ${open}`,
    `version = conn.execute("PRAGMA user_version").fetchone()[0]`,
    `named = "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"`,
    `tables = [row[0] for row in conn.execute(named)]`,
    `counts = {t: conn.execute("SELECT count(*) FROM [" + t + "]").fetchone()[0] for t in tables}`,
    `print(json.dumps({"user_version": version, "counts": counts}, sort_keys=True))`,
  ].join("\n");
  const done = uv(home, ["python", "-c", script]);
  if (done.status !== 0) throw new Error(`reading the ledger under ${home} failed: ${done.stderr}`);
  return JSON.parse(done.stdout) as Ledger;
}

/**
 * The rows that appeared between two readings, table by table, naming only the tables that moved.
 * A `user_version` that moved is reported as a table of its own, so one diff says both.
 */
export function rowDelta(before: Ledger, after: Ledger): Record<string, number> {
  const moved: Record<string, number> = {};
  const tables = new Set([...Object.keys(before.counts), ...Object.keys(after.counts)]);
  for (const table of [...tables].sort()) {
    const change = (after.counts[table] ?? 0) - (before.counts[table] ?? 0);
    if (change !== 0) moved[table] = change;
  }
  if (after.user_version !== before.user_version) {
    moved["PRAGMA user_version"] = after.user_version - before.user_version;
  }
  return moved;
}

/**
 * The seeded violation the console and models walks must catch: one extra row, written by the
 * spec rather than by the page, through `db.connect()` and `db.write` like any writer in the
 * package. It goes into `mem_episodic`, a table a run writes too, so the console walk has to tell
 * "one row more of a table that moved anyway" from a clean walk, not only "a table that moved".
 */
export function seedExtraRow(home: string = harnessHome()): void {
  const script = [
    `import uuid`,
    `from harnessed.memory import db`,
    `conn = db.connect()`,
    `sql = "INSERT INTO mem_episodic (episode_id, run_id, kind, text, created_at) VALUES (?, ?, ?, ?, ?)"`,
    `row = ("seeded-" + uuid.uuid4().hex, "seeded-149", "seeded", "ticket 149's seeded extra row", "2026-09-25T00:00:00Z")`,
    `db.write(conn, ((sql, row),))`,
  ].join("\n");
  const done = uv(home, ["python", "-c", script]);
  if (done.status !== 0) throw new Error(`seeding a row under ${home} failed: ${done.stderr}`);
}

// ---- The request log's one widening (ratified 2026-10-11, Q7, widening d; ticket 295) -----------

/**
 * Does the e2e request log admit this request as a read it need not count as a write? Only a
 * `HEAD` does, and only when it is same-origin and off `/v1` (Next's export-mode prefetch of a
 * linked page). A `HEAD` to the gateway or to another origin is not admitted. Every walk that lets
 * a `HEAD` through goes by this one predicate, so the widening cannot grow in one spec unseen.
 */
export function admitsHead(method: string, url: string): boolean {
  if (method !== "HEAD") return false;
  const target = new URL(url);
  return target.origin === DASHBOARD_URL && !target.pathname.startsWith("/v1");
}

/** Is this request a write by FR-53's reading: anything but a `GET` that the log does not admit? */
export function isWrite(method: string, url: string): boolean {
  return method !== "GET" && !admitsHead(method, url);
}
