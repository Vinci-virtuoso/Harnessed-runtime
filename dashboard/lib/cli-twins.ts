/**
 * The CLI `--json` twin of a gateway route (FR-51, `D-4`).
 *
 * `ux/experience.md` lists the commands that print the same payload the gateway serves, because
 * the gateway reuses the CLI's own `json_payload(...)` shapes rather than reverse-engineering Rich
 * output (architecture, "Every decision-producing command supports `--json`"). `ProvenanceDrawer`
 * shows the twin beside the `curl` line so an operator can leave the window without losing the
 * thread.
 *
 * Static copy, keyed on a route template. Nothing here is computed and **no id is assembled**: a
 * command is shown exactly as `experience.md` spells it, placeholders included, because building
 * an id client-side is MUST rule 25's anti-pattern. A route with no twin gets nothing rather than
 * a plausible guess.
 */

/** One route template and the command that prints the same payload. `{id}` is one path segment. */
export type CliTwin = { route: string; command: string };

/**
 * Every twin `ux/experience.md` lists, the eight reads ticket 232 gave a twin, the v3 surfaces that gained one after it, the six v4
 * aggregates (tickets 126 to 133) and the Models page's four (tickets 147, 148 and 186). The
 * first match wins, so the two fixed `/v1/runs/...` routes sit above `/v1/runs/{id}`, which
 * would otherwise claim them as a run key.
 */
export const CLI_TWINS: readonly CliTwin[] = [
  { route: "/v1/runs/totals", command: "harnessed runs totals --json" },
  { route: "/v1/runs/turn_seconds", command: "harnessed runs turn-seconds --json" },
  { route: "/v1/spend", command: "harnessed spend --json" },
  { route: "/v1/events/totals", command: "harnessed events totals --json" },
  { route: "/v1/events/gate", command: "harnessed events gate --json" },
  { route: "/v1/memory/totals", command: "harnessed memory totals --json" },
  { route: "/v1/memory/consolidation", command: "harnessed memory consolidate --list --json" },
  { route: "/v1/prices", command: "harnessed prices list --json" },
  { route: "/v1/providers", command: "harnessed providers list --json" },
  { route: "/v1/agents", command: "harnessed agents list --json" },
  { route: "/v1/permissions", command: "harnessed permissions list --state pending --json" },
  { route: "/v1/tasks/totals", command: "harnessed task totals --json" },
  { route: "/v1/tasks", command: "harnessed task list --json" },
  { route: "/v1/tasks/{id}", command: "harnessed task show <task_id> --json" },
  { route: "/v1/schedules", command: "harnessed schedule list --json" },
  { route: "/v1/router", command: "harnessed router status --json" },
  {
    route: "/v1/sessions/{id}/decisions",
    command: "harnessed decisions list <session_id> --json",
  },
  {
    route: "/v1/sessions/{id}/messages",
    command: "harnessed sessions messages <session_id> --json",
  },
  { route: "/v1/messages", command: "harnessed sessions recent --json" },
  { route: "/v1/health", command: "harnessed health --json" },
  { route: "/v1/runs", command: "harnessed runs list --json" },
  { route: "/v1/promotions", command: "harnessed improve promotions --json" },
  { route: "/v1/runs/{id}/trace", command: "harnessed runs trace <run_key> --json" },
  { route: "/v1/sessions/{id}/events", command: "harnessed sessions events <session_id> --json" },
  { route: "/v1/sessions/{id}/stream", command: "harnessed sessions stream <session_id>" },
  { route: "/v1/memory/facts/{id}", command: "harnessed memory fact <mem_id> --json" },
  { route: "/v1/improve/cycles/{id}", command: "harnessed improve show <id> --json" },
  { route: "/v1/runs/{id}", command: "harnessed run --json" },
  { route: "/v1/eval/runs", command: "harnessed eval list --json" },
  { route: "/v1/eval/suites", command: "harnessed eval suites --json" },
  { route: "/v1/eval/runs/{id}", command: "harnessed eval show <id> --json" },
  { route: "/v1/eval/compare", command: "harnessed eval compare <before> <after> --json" },
  { route: "/v1/sessions", command: "harnessed sessions list --json" },
  { route: "/v1/memory/facts", command: "harnessed memory search --json" },
  { route: "/v1/memory/procedures", command: "harnessed memory procedures --json" },
  { route: "/v1/memory/episodes/{id}", command: "harnessed memory episodes --json" },
  { route: "/v1/outputs/{id}", command: "harnessed outputs get <output_id> --out <path>" },
];

/** A concrete route, without the query string a template never spells. */
function pathOf(route: string): string {
  const [before] = route.split("?");
  return before ?? route;
}

/** Does a route template describe this route? `{id}` stands for exactly one segment. */
function routeMatches(template: string, route: string): boolean {
  const wanted = template.split("/");
  const asked = pathOf(route).split("/");
  if (wanted.length !== asked.length) return false;
  return wanted.every((segment, index) => segment === "{id}" || segment === asked[index]);
}

/** The command that prints this route's payload, or `undefined` where no CLI surface does. */
export function cliTwinFor(route: string): string | undefined {
  return CLI_TWINS.find((twin) => routeMatches(twin.route, route))?.command;
}
