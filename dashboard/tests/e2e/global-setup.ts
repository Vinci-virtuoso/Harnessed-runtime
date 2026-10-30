/**
 * Seeds the run's temporary `HARNESSED_HOME` through the CLI under the fake provider (decision
 * 30). The gateway is already serving that directory: Playwright starts `webServer` before
 * `globalSetup`, and the gateway opens the database per request, so it serves what this writes.
 */
import { spawnSync } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";

import { HOME_VARIABLE, REPO_ROOT } from "./harness";

export default function globalSetup(): void {
  const home = process.env[HOME_VARIABLE];
  if (!home) throw new Error(`${HOME_VARIABLE} is not set; playwright.config.ts sets it`);
  const seeded = spawnSync("uv", ["run", "harnessed", "eval", "run", "smoke", "--json"], {
    cwd: REPO_ROOT,
    env: { ...process.env, HARNESSED_HOME: home, HARNESSED_PROVIDER: "fake" },
    encoding: "utf8",
    shell: false,
  });
  if (seeded.status !== 0) {
    throw new Error(`seeding ${home} failed (${seeded.status}): ${seeded.stderr}${seeded.stdout}`);
  }
  seedOutputsSession(home);
  seedTraceRows(home);
  seedTraceTasks(home);
  seedPendingPermission(home);
}

// ---- ticket 253, `permission.spec.ts`'s own seed: a task paused on one agent request -------------

/** The session, task and request `permission.spec.ts` opens, answers and reads back. */
export const PERMISSION_SESSION = "run_000000000000000PERMSEED0";
export const PERMISSION_TASK_TEXT = "run the release checks";
export const PERMISSION_DESCRIPTION = "git log --oneline -20";
export const PERMISSION_AGENT = "stub-agent";

function permissionSeedPath(home: string): string {
  return join(dirname(home), "permission-seed.json");
}

/** The permission id the seed recorded, written by this file's own seed step. */
export function seededPermissionId(home: string): string {
  return (JSON.parse(readFileSync(permissionSeedPath(home), "utf8")) as { permissionId: string }).permissionId;
}

/**
 * One task paused on a pending permission, written through the same writers a run calls
 * (`tasks/store.py`, `acp/pending.py`, the router ledger), as ticket 256's `seed_tasks` does for the
 * recorded fixtures. The ceiling is far in the future, so the request stays pending until the spec
 * answers it, and the message's `run.finished` is published so the session reads as at rest.
 */
function seedPendingPermission(home: string): void {
  const script = [
    "from decimal import Decimal",
    "from harnessed.acp import pending",
    "from harnessed.events.bus import EventBus",
    "from harnessed.memory import db",
    "from harnessed.router import decisions",
    "from harnessed.tasks import store",
    "conn = db.connect()",
    `session = ${JSON.stringify(PERMISSION_SESSION)}`,
    `text = ${JSON.stringify(PERMISSION_TASK_TEXT)}`,
    'task_id = str(store.queue(conn, text, via="cli")["task_id"])',
    "bus = EventBus(run_id=session, conn=conn)",
    'seq = bus.publish("run.started", {"provider": "fake"}).seq',
    'store.begin_message(conn, task_id, run_id=session, message_seq=seq, author="task", first=True)',
    "decisions.record_decision(conn, run_id=session, message_seq=seq, task=text, model='seeded-router', hop=0,",
    "    latency_seconds='0.000', outcome='routed', route='stub-agent', handler='agent', confidence=Decimal('0.9'),",
    "    floor=Decimal('0.6'), needs_memory=None, memory_kind=None, stop_reason=None, fallback_cause=None,",
    "    state_truncated=False, answered=None)",
    "request = pending.record(conn, session_id=session, agent=" + JSON.stringify(PERMISSION_AGENT) + ", tool_kind='execute',",
    `    description=${JSON.stringify(PERMISSION_DESCRIPTION)}, ceiling_at='2099-01-01T00:00:00Z', message_seq=seq, hop=0)`,
    "store.pause_for_permission(conn, task_id, request.permission_id)",
    'bus.publish("run.finished", {"guardrail": None, "stop_reason": "end_turn"})',
    "print(request.permission_id)",
  ].join("\n");
  const done = spawnSync("uv", ["run", "python", "-c", script], {
    cwd: REPO_ROOT,
    env: { ...process.env, HARNESSED_HOME: home },
    encoding: "utf8",
    shell: false,
  });
  const permissionId = done.stdout.trim();
  if (done.status !== 0 || !permissionId) {
    throw new Error(`seeding a pending permission under ${home} failed: ${done.stderr}${done.stdout}`);
  }
  writeFileSync(permissionSeedPath(home), JSON.stringify({ permissionId }), "utf8");
}

/** Sessions `glass.spec.ts` needs so Traces at `?limit=100` renders 100 rows (ticket 250, G-5). */
export const TRACE_ROWS = 100;

/**
 * One-event-long runs, written through `db.write` like any writer in the package: each is a
 * `run.started` and its `run.finished`, so the sessions listing counts it and a row it renders
 * is a closed session. Fixed timestamps, so the listing is the same listing on every run.
 */
function seedTraceRows(home: string): void {
  const script = [
    "from harnessed.memory import db",
    "conn = db.connect()",
    'sql = "INSERT INTO events (run_id, seq, name, payload, created_at) VALUES (?, ?, ?, ?, ?)"',
    "rows = []",
    `for n in range(${TRACE_ROWS}):`,
    '    run_id = f"glass-seed-{n:03d}"',
    '    stamp = f"2026-10-0{1 + n % 3}T0{n % 10}:00:{n % 60:02d}Z"',
    '    rows.append((sql, (run_id, 1, "run.started", "{}", stamp)))',
    '    rows.append((sql, (run_id, 2, "run.finished", "{}", stamp)))',
    "db.write(conn, tuple(rows))",
  ].join("\n");
  const done = spawnSync("uv", ["run", "python", "-c", script], {
    cwd: REPO_ROOT,
    env: { ...process.env, HARNESSED_HOME: home },
    encoding: "utf8",
    shell: false,
  });
  if (done.status !== 0) {
    throw new Error(`seeding ${TRACE_ROWS} trace rows under ${home} failed: ${done.stderr}${done.stdout}`);
  }
}

/** Queued tasks, so Traces at `?limit=100` also renders 100 task rows (ticket 301, decision 103). */
function seedTraceTasks(home: string): void {
  const script = [
    "from harnessed.memory import db",
    "from harnessed.tasks import store",
    "conn = db.connect()",
    `for n in range(${TRACE_ROWS}):`,
    '    store.queue(conn, f"tab stop seed task {n:03d}", via="cli")',
  ].join("\n");
  const done = spawnSync("uv", ["run", "python", "-c", script], {
    cwd: REPO_ROOT,
    env: { ...process.env, HARNESSED_HOME: home },
    encoding: "utf8",
    shell: false,
  });
  if (done.status !== 0) {
    throw new Error(`seeding ${TRACE_ROWS} trace tasks under ${home} failed: ${done.stderr}${done.stdout}`);
  }
}

// ---- ticket 189, `outputs.spec.ts`'s own seed: a real routed chain producing one `ui` output ---

/** The router entry a `stub-ui` route delegates to: `kind = "cli"`, the real
 * `tests/support/stub_cli_agent.py` (ticket 181's own stub), task on stdin, write-jail disabled
 * so the stub's own written file survives, `outputs = "*.html"` so the delegation's own glob
 * picks it up (`acp/outputs.py::record_hop`, position 1 - position 0 stays the delegation's
 * plain-text summary). No `produces`: `text/html` already answers `ui`
 * (`acp/outputs.py::KIND_BY_MEDIA_TYPE`). */
export const STUB_UI_AGENT = "stub-ui";
export const UI_OUTPUT_FILENAME = "preview.html";

/** The name a script inside the seeded output would set on the real page, if the sandbox ever
 * let it run. `outputs.spec.ts` asserts this stays `undefined` on the console page itself. */
export const UI_SCRIPT_FLAG = "harnessedTicket189ScriptRan";

export const UI_OUTPUT_MARKUP =
  '<p data-testid="seeded-wireframe">Ticket 189 seeded wireframe</p>' +
  `<script>window.parent.${UI_SCRIPT_FLAG} = true;</script>`;

/** The exact task text `src/harnessed/corpus/wire/jev/12659270cc4c` was recorded to answer
 * (`_index.yaml`'s own note): `router/state.py` labels the router's state `Task: <this text>`,
 * and `FakeDecider.scope_of` hashes that line to the fixture's own directory name when a caller
 * declares no scope - `sha256("render the ticket 189 seeded preview")[:12] ==
 * "12659270cc4c"`. Sent through a real `harnessed run`, never a `ScopedFakeDecider` injection. */
// Ticket 198: the chain ends in a Groq synthesis pass, so this prompt needs the committed
// src/harnessed/corpus/wire/fake/12659270cc4c fixture (the reply is that pass; the ui output is a hop).
const SEED_PROMPT = "render the ticket 189 seeded preview";

function outputsSeedPath(home: string): string {
  return join(dirname(home), "outputs-seed.json");
}

/** The session `outputs.spec.ts` opens: the `run_id` `seedOutputsSession` discovered, written by
 * this file's own seed step so the spec never has to re-derive or guess it. */
export function seededOutputsSessionId(home: string): string {
  const raw = readFileSync(outputsSeedPath(home), "utf8");
  return (JSON.parse(raw) as { sessionId: string }).sessionId;
}

function agentsToml(pythonExecutable: string): string {
  const stub = join(REPO_ROOT, "tests", "support", "stub_cli_agent.py");
  return [
    `[agents.${STUB_UI_AGENT}]`,
    `command = ${JSON.stringify(pythonExecutable)}`,
    `args = [${JSON.stringify(stub)}]`,
    'kind = "cli"',
    'input = "stdin"',
    "allow_write = true",
    'outputs = "*.html"',
    "",
  ].join("\n");
}

/** The uv-managed interpreter `tests/support/stub_cli_agent.py` runs under - resolved once,
 * rather than assumed at a fixed `.venv` path, since only `uv` itself knows where it put it. */
function pythonExecutable(): string {
  const found = spawnSync("uv", ["run", "python", "-c", "import sys; print(sys.executable)"], {
    cwd: REPO_ROOT,
    encoding: "utf8",
    shell: false,
  });
  const path = found.stdout.trim();
  if (found.status !== 0 || !path) {
    throw new Error(`resolving the uv-managed python executable failed: ${found.stderr}`);
  }
  return path;
}

/**
 * A real `harnessed run` under a temporary `agents.toml`, its own working directory beside the
 * harness home (never `REPO_ROOT`, so it never picks up an operator's own `agents.toml`; never
 * inside `home`, so a ledger row count taken there is untouched). `FakeDecider` replays
 * `src/harnessed/corpus/wire/jev/12659270cc4c` (`HARNESSED_PROVIDER=fake` selects it the same way it
 * selects `FakeProvider`): its `base` variant routes to `stub-ui`, its `done` variant ends the
 * chain once the delegation's own `Previous step:` section is in the router's state, so the
 * chain runs exactly one hop. The reply is `harnessed run`'s own text echo of the task; the
 * seeded UI file is that one hop's `outputs` glob match, read back afterwards from the ledger's
 * own `outputs` table by its unique seeded name rather than parsed off the CLI's stdout.
 */
function seedOutputsSession(home: string): void {
  const cwd = join(dirname(home), "outputs-seed");
  mkdirSync(cwd, { recursive: true });
  writeFileSync(join(cwd, "agents.toml"), agentsToml(pythonExecutable()), "utf8");

  const run = spawnSync("uv", ["run", "harnessed", "run", SEED_PROMPT], {
    cwd,
    env: {
      ...process.env,
      HARNESSED_HOME: home,
      HARNESSED_PROVIDER: "fake",
      UV_PROJECT: REPO_ROOT,
      HARNESSED_STUB_WRITE_FILES: JSON.stringify({ [UI_OUTPUT_FILENAME]: UI_OUTPUT_MARKUP }),
    },
    encoding: "utf8",
    shell: false,
  });
  if (run.status !== 0) {
    throw new Error(
      `seeding the outputs session under ${home} failed (${run.status}): ${run.stderr}${run.stdout}`,
    );
  }

  const script = [
    "from harnessed.memory import db",
    "conn = db.connect_readonly(db.database_path())",
    `row = conn.execute("SELECT run_id FROM outputs WHERE name = ?", (${JSON.stringify(UI_OUTPUT_FILENAME)},)).fetchone()`,
    "print(row[0] if row else '')",
  ].join("\n");
  const found = spawnSync("uv", ["run", "python", "-c", script], {
    cwd: REPO_ROOT,
    env: { ...process.env, HARNESSED_HOME: home },
    encoding: "utf8",
    shell: false,
  });
  const sessionId = found.stdout.trim();
  if (found.status !== 0 || !sessionId) {
    throw new Error(`reading the seeded outputs session's id failed: ${found.stderr}${found.stdout}`);
  }
  writeFileSync(outputsSeedPath(home), JSON.stringify({ sessionId }), "utf8");
}
