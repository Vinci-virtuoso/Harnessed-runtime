// @vitest-environment jsdom
/**
 * `components/RegistryCard.tsx` (FR-121, ticket 186): the tags and gloss states the recorded
 * `/v1/agents` fixture never carries a row for - a `cli` entry, `confined: false`, a populated
 * `billing` of each word, and non-empty `use_for`/`not_for` text. The badge, the empty-`use_for`
 * warning and the missing-`billing` row are covered against the recorded fixture in
 * `models.test.tsx`; this file hand-builds its own rows the same way `provider-card.test.tsx`
 * hand-builds a leaf, so it is a component test and not a recorded gateway fixture (decision 33
 * covers a route's own response body, not an isolated component's props).
 */
import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  COPY_LABEL,
  NOT_FOR_LABEL,
  NO_USE_FOR,
  RegistryCard,
  UNCONFINED,
  UNCONFINED_GLOSS,
  confinementLine,
} from "../../components/RegistryCard";
import { humanWord } from "../../lib/human-words";
import type { Leaf, Wire } from "../../lib/wire";

const ROUTE = "/v1/agents";

function leaf(kind: Leaf["kind"], text: string, path: string): Leaf {
  return { kind, text, path: `.agents[0]${path}`, route: ROUTE };
}

type Row = {
  agent: string;
  status: string;
  fingerprintMatches: string;
  probed: string;
  kind: string;
  billing: string | null;
  useFor: string;
  notFor: string;
  confined: string;
  settings?: string | null;
  jail?: string | null;
  reason?: string | null;
};

function nullable(value: string | null | undefined, path: string): Leaf {
  return value === null || value === undefined
    ? leaf("null", "null", path)
    : leaf("string", value, path);
}

/** One row of `/v1/agents`, scoped exactly as `models/panels.tsx::rowWire` scopes it. */
function wireOf(row: Row): Wire {
  const fields: Record<string, Leaf> = {
    ".agent": leaf("string", row.agent, ".agent"),
    ".status": leaf("string", row.status, ".status"),
    ".fingerprint_matches": leaf(
      "boolean",
      row.fingerprintMatches,
      ".fingerprint_matches",
    ),
    ".probed": leaf("boolean", row.probed, ".probed"),
    ".kind": leaf("string", row.kind, ".kind"),
    ".billing":
      row.billing === null
        ? leaf("null", "null", ".billing")
        : leaf("string", row.billing, ".billing"),
    ".use_for": leaf("string", row.useFor, ".use_for"),
    ".not_for": leaf("string", row.notFor, ".not_for"),
    ".confined": leaf("boolean", row.confined, ".confined"),
    ".settings": nullable(row.settings, ".settings"),
    ".jail": nullable(row.jail, ".jail"),
    ".confined_reason": nullable(row.reason, ".confined_reason"),
  };
  return {
    route: ROUTE,
    text: "{}",
    byteLength: "2",
    at: (path: string) => {
      const found = fields[path];
      if (found === undefined) throw new Error(`no leaf stubbed for ${path}`);
      return found;
    },
  };
}

const CLI_UNCONFINED: Row = {
  agent: "sketch",
  status: "passed",
  fingerprintMatches: "true",
  probed: "true",
  kind: "cli",
  billing: "openrouter",
  useFor: "drafting release notes",
  notFor: "anything touching a browser",
  confined: "false",
};

const ACP_SUBSCRIPTION: Row = {
  agent: "claude-code",
  status: "passed",
  fingerprintMatches: "true",
  probed: "true",
  kind: "acp",
  billing: "oauth",
  useFor: "editing files in the working directory",
  notFor: "",
  confined: "true",
};

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("RegistryCard's tags", () => {
  it("reads a cli entry as Command-line skill, Unconfined with its gloss, and OpenRouter metered", () => {
    render(<RegistryCard entry={wireOf(CLI_UNCONFINED)} />);
    const card = screen.getByTestId("registry-card");
    expect(within(card).getByText(humanWord("kind", "cli") ?? "")).toBeTruthy();
    expect(humanWord("kind", "cli")).toBe("Command-line skill");
    expect(within(card).getByText(UNCONFINED)).toBeTruthy();
    expect(within(card).getByText(UNCONFINED_GLOSS)).toBeTruthy();
    expect(
      within(card).getByText(humanWord("billing", "openrouter") ?? ""),
    ).toBeTruthy();
    expect(humanWord("billing", "openrouter")).toBe("OpenRouter, metered");
    expect(card.textContent).toContain(CLI_UNCONFINED.useFor);
    expect(card.textContent).toContain(
      `${NOT_FOR_LABEL} ${CLI_UNCONFINED.notFor}`,
    );
    expect(card.textContent).not.toContain(NO_USE_FOR);
  });

  it("reads an acp entry as Live agent, confined (no Unconfined tag), and Subscription", () => {
    render(<RegistryCard entry={wireOf(ACP_SUBSCRIPTION)} />);
    const card = screen.getByTestId("registry-card");
    expect(within(card).getByText(humanWord("kind", "acp") ?? "")).toBeTruthy();
    expect(humanWord("kind", "acp")).toBe("Live agent");
    expect(
      within(card).getByText(humanWord("billing", "oauth") ?? ""),
    ).toBeTruthy();
    expect(humanWord("billing", "oauth")).toBe("Subscription");
    expect(within(card).queryByText(UNCONFINED)).toBeNull();
    expect(within(card).queryByText(UNCONFINED_GLOSS)).toBeNull();
    // An empty `not_for` renders no "Not for:" line at all.
    expect(card.textContent).not.toContain(NOT_FOR_LABEL);
  });

  it("copies the agent-test line, never a test button", () => {
    const writeText = vi.fn(async () => {});
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    render(<RegistryCard entry={wireOf(ACP_SUBSCRIPTION)} />);
    const card = screen.getByTestId("registry-card");
    const command = within(card).getByTestId("agent-test-command");
    expect(command.textContent).toBe(
      `harnessed agents test ${ACP_SUBSCRIPTION.agent}`,
    );
    fireEvent.click(within(card).getByRole("button", { name: COPY_LABEL }));
    expect(writeText).toHaveBeenCalledWith(
      `harnessed agents test ${ACP_SUBSCRIPTION.agent}`,
    );
    expect(
      within(card)
        .queryAllByRole("button", { name: /\btest\b/i })
        .filter((button) => button.textContent !== COPY_LABEL),
    ).toEqual([]);
  });
});

describe("RegistryCard's confinement line (FR-131, G-60)", () => {
  const cases: Array<[string, Partial<Row>, string]> = [
    ["harnessed", { settings: "harnessed" }, "Runs with Harnessed's settings"],
    [
      "folder",
      { settings: "folder", reason: "its adapter documents no override" },
      "Runs with its folder's settings: its adapter documents no override",
    ],
    [
      "job object",
      { jail: "job_object" },
      "Jailed: Job Object, limits its processes only",
    ],
    ["bwrap", { jail: "bwrap" }, "Jailed: bwrap"],
    [
      "none",
      { jail: "none", reason: "user namespaces refused" },
      "Unconfined: bwrap could not start here (user namespaces refused)",
    ],
  ];

  it.each(cases)(
    "reads the %s case from the served fields only",
    (_name, fields, text) => {
      render(
        <RegistryCard entry={wireOf({ ...ACP_SUBSCRIPTION, ...fields })} />,
      );
      expect(screen.getByTestId("confinement-line").textContent).toBe(text);
    },
  );

  it("warns only for the unconfined case", () => {
    const warn = (row: Partial<Row>) =>
      confinementLine(
        wireOf({ ...ACP_SUBSCRIPTION, ...row }).at(".settings"),
        wireOf({ ...ACP_SUBSCRIPTION, ...row }).at(".jail"),
        wireOf({ ...ACP_SUBSCRIPTION, ...row }).at(".confined_reason"),
      ).warn;
    expect(warn({ jail: "none", reason: "x" })).toBe(true);
    expect(warn({ jail: "bwrap" })).toBe(false);
    expect(warn({ settings: "harnessed" })).toBe(false);
  });
});
