// @vitest-environment jsdom
/**
 * FR-89 (UJ-17, ticket 147): `/models` renders provider state as the gateway states it.
 *
 * Two stacked sections, each its own `PanelFrame` with its own heading and its own route:
 * **Providers** over `GET /v1/providers` and **Prices** over `GET /v1/prices` (decision 55 keeps
 * `priced_models` off the providers route, so the page reads prices as a second panel). Each
 * provider row is a `ProviderCard`: `configured` as a `PassMark` printing the verbatim field, never
 * "connected" or "active" (`D-7`), `key_var` and `default_model` as `Value`s in mono. An
 * unconfigured card holds ticket 146's `CredentialField` open; a configured one keeps it behind a
 * "rotate key" text action; a provider that takes no key has no field at all.
 *
 * The page's controls are counted: 0 pin, star, "make default" or model-switch affordances, and
 * the only write is ticket 146's. The sidebar is one list, Overview to Models, with Models directly
 * beneath Sessions.
 *
 * The inputs are the recorded `/v1/providers`, `/v1/prices` and `empty.prices` bodies (decision 33,
 * MUST rule 24); nothing here edits one. The control app's 200 has no recorder (ticket 139's
 * learning), so its body is the route's own shape, written below and marked where it is.
 * This suite renders no pixels: the Models screenshots belong to the models walk (149) and FR-91's
 * by-eye record (150). It writes the census and the card states to `evidence/147/` as text.
 *
 * FR-90 (ticket 148) added the third section, **Agents** over `GET /v1/agents`: one card per row,
 * reading `live` only on a pass whose fingerprint matches, `stale`, `failed` or `unproven`, each a
 * word and never a colour alone; 0 test buttons and one copyable `harnessed agents test <name>`
 * line per card; zero rows name `harnessed agents add`.
 *
 * FR-121 (ticket 186) replaces that section with **Router and agents**: `RouterCard` over
 * `GET /v1/router` first, at `#router` where the Overview diagram's delegation box links, then
 * `RegistryCard` per `/v1/agents` row (`AgentCard`'s replacement - the probe badge is kept). Both
 * cards are entirely read-only; every affordance is a copy line, never a button that runs
 * anything. FR-129 closes the page with `SystemSection`, a collapsed disclosure holding the v3
 * health panel moved off the Overview. Evidence goes to `evidence/186/`.
 */
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";

import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterAll, afterEach, describe, expect, it, vi } from "vitest";

import { AGENTS_TITLE, PRICES_TITLE, PROVIDERS_TITLE, ROUTER_TITLE } from "../../app/models/panels";
import ModelsPage from "../../app/models/page";
import { AppShell, PAGES } from "../../components/AppShell";
import { ROTATE_KEY, SAVE, credentialLabel } from "../../components/ProviderCard";
import {
  COPY_LABEL as REGISTRY_COPY_LABEL,
  NO_USE_FOR,
  REGISTRY_WORDS,
} from "../../components/RegistryCard";
import {
  COPY_LABEL as ROUTER_COPY_LABEL,
  LIVE_RUNG_COMMAND,
  ROUTER_WORDS,
} from "../../components/RouterCard";
import { SYSTEM_TITLE } from "../../components/SystemSection";
import { PROVIDERS_ROUTE as CONTROL_PROVIDERS_ROUTE } from "../../lib/control-client";
import { humanWord } from "../../lib/human-words";

import { figuresWithoutSource } from "../support/figures";
import { divergences, renderedFigures } from "../support/source-at";

const DASHBOARD = resolve(import.meta.dirname, "../..");
const REPO = resolve(DASHBOARD, "..");
const GATEWAY_FIXTURES = resolve(DASHBOARD, "tests/fixtures/gateway");
const EVIDENCE = resolve(REPO, "evidence/147");
const EVIDENCE_186 = resolve(REPO, "evidence/186");

const PROVIDERS = "/v1/providers";
const PRICES = "/v1/prices";
const AGENTS = "/v1/agents";
const ROUTER = "/v1/router";
const HEALTH = "/v1/health";

type Recorded = { route: string; code: number; text: string };

/** One recorded response, read from the committed record and never written by a test. */
function recorded(name: string): Recorded {
  const meta = JSON.parse(readFileSync(resolve(GATEWAY_FIXTURES, `${name}.meta.json`), "utf8")) as {
    route: string;
    status: number;
  };
  return {
    route: meta.route,
    code: meta.status,
    text: readFileSync(resolve(GATEWAY_FIXTURES, `${name}.body`), "utf8"),
  };
}

const providers = recorded("providers");
const prices = recorded("prices");
const noPrices = recorded("empty.prices");

const SEARCH = "Search models";
const HINT = "Type a model name to see its stored price.";
const sentPrices = (
  JSON.parse(prices.text) as { prices: Record<string, string | null>[] }
).prices;

function typeQuery(panel: HTMLElement, value: string) {
  fireEvent.change(within(panel).getByLabelText(SEARCH), { target: { value } });
}

const agents = recorded("agents");
const noAgents = recorded("empty.agents");
const router = recorded("router");
const routerProbed = recorded("router.probed");
const routerFailed = recorded("router.failed");
const health = recorded("health");
/** Decision 24's 500, as the gateway recorded it for a home with no database file. */
const storageError = recorded("nodatabase.runs.listing");

/** The control app's 200 for a saved key: the route's own shape (no recorder, ticket 139). */
const SAVED_BODY =
  '{"credential_live":true,"credential_var":"GROQ_API_KEY","env_file":".env","provider":"groq","rungs":[],"unchanged":false}';

const TYPED = "not-a-real-value-27182818284590452353";

// ---- One answer per route, so nothing here is a network ----------------------------------------

type Answer = { status: number; text: string };
type Route = Answer | "rejects";

function answer({ status, text }: Answer): Response {
  return {
    status,
    ok: status >= 200 && status < 300,
    headers: { get: () => null },
    text: async () => text,
  } as unknown as Response;
}

/** Stub `fetch` with one answer (or a queue of answers) per URL; hand back the spy. */
function answersWith(routes: Record<string, Route | Route[]>): ReturnType<typeof vi.fn> {
  const spy = vi.fn(async (url: string) => {
    const entry = routes[url];
    const next = Array.isArray(entry) ? (entry.length > 1 ? entry.shift() : entry[0]) : entry;
    if (next === undefined) throw new Error(`no answer stubbed for ${url}`);
    if (next === "rejects") throw new Error("ECONNREFUSED");
    return answer(next);
  });
  vi.stubGlobal("fetch", spy);
  return spy;
}

const recordedRoutes = (overrides: Record<string, Route | Route[]> = {}) => ({
  [HEALTH]: { status: health.code, text: health.text },
  [PROVIDERS]: { status: providers.code, text: providers.text },
  [PRICES]: { status: prices.code, text: prices.text },
  [AGENTS]: { status: agents.code, text: agents.text },
  [ROUTER]: { status: router.code, text: router.text },
  ...overrides,
});

async function settle(): Promise<void> {
  await act(async () => {});
}

async function renderModels() {
  const rendered = render(<AppShell>{ModelsPage()}</AppShell>);
  await settle();
  return rendered;
}

function section(title: string): HTMLElement {
  return screen.getByRole("region", { name: title });
}

function card(provider: string): HTMLElement {
  return within(section(PROVIDERS_TITLE)).getByRole("region", { name: provider });
}

const log: string[] = [];
const registryLog: string[] = [];

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

afterAll(() => {
  mkdirSync(dirname(resolve(EVIDENCE, "models.txt")), { recursive: true });
  writeFileSync(resolve(EVIDENCE, "models.txt"), `${log.join("\n")}\n`, "utf8");
  mkdirSync(EVIDENCE_186, { recursive: true });
  writeFileSync(resolve(EVIDENCE_186, "router-and-registry.txt"), `${registryLog.join("\n")}\n`, "utf8");
});

// ---- AC 3: stacked sections, each its own heading and its own route ---------------------------

describe("/models", () => {
  it("is four stacked sections, Providers, Prices, Router then Agents, each reading its own route", async () => {
    const spy = answersWith(recordedRoutes());
    const { container } = await renderModels();
    // The Prices panel shows a figure only once a name is searched (ticket 202).
    typeQuery(section(PRICES_TITLE), "/");

    expect(screen.getByRole("heading", { level: 1, name: "Models" })).toBeTruthy();
    const regions = [
      section(PROVIDERS_TITLE),
      section(PRICES_TITLE),
      section(ROUTER_TITLE),
      section(AGENTS_TITLE),
    ];
    for (let index = 0; index < regions.length - 1; index += 1) {
      expect(regions[index]?.compareDocumentPosition(regions[index + 1] as Node)).toBe(
        Node.DOCUMENT_POSITION_FOLLOWING,
      );
    }
    for (const [region, route] of [
      [regions[0], PROVIDERS],
      [regions[1], PRICES],
      [regions[2], ROUTER],
      [regions[3], AGENTS],
    ] as const) {
      const routes = new Set(
        [...(region as HTMLElement).querySelectorAll("[data-figure]")].map((figure) =>
          figure.getAttribute("data-route"),
        ),
      );
      expect([...routes]).toEqual([route]);
    }
    const asked = spy.mock.calls.map(([url]) => url).filter((url) => url !== HEALTH);
    expect(asked.sort()).toEqual([AGENTS, PRICES, PROVIDERS, ROUTER]);

    expect(figuresWithoutSource(container)).toEqual([]);
    // `.kind` and `.billing` render a human word (`lib/human-words.ts`), never the raw wire word,
    // so they are excluded from this literal-characters oracle the same way `overview.test.tsx`
    // excludes `key_kind` - their own test below checks the human word against the raw tooltip.
    const humanized = new Set([".routing", ".agents[0].kind", ".agents[1].kind", ".agents[2].kind", ".agents[3].kind"]);
    const literal = renderedFigures(container).filter((figure) => !humanized.has(figure.path));
    expect(
      divergences(
        literal,
        new Map([
          [PROVIDERS, providers.text],
          [PRICES, prices.text],
          [ROUTER, router.text],
          [AGENTS, agents.text],
        ]),
      ),
    ).toEqual([]);
  });

  // ---- AC 3: each row is a ProviderCard, configured verbatim, default_model in mono ------------

  it("renders one ProviderCard per row with configured as a verbatim PassMark", async () => {
    answersWith(recordedRoutes());
    await renderModels();

    const names = (JSON.parse(providers.text) as { providers: { name: string }[] }).providers.map(
      (row) => row.name,
    );
    const cards = within(section(PROVIDERS_TITLE)).getAllByTestId("provider-card");
    expect(cards.map((element) => element.querySelector("h3")?.textContent)).toEqual(names);

    const states: string[] = [];
    for (const [index, element] of cards.entries()) {
      const mark = within(element).getByTestId("pass-mark");
      expect(mark.textContent).toMatch(/^configured: (true|false)/);
      expect(mark.querySelector("[data-figure]")?.getAttribute("data-path")).toBe(
        `.providers[${index}].configured`,
      );
      const model = element.querySelector(`[data-path=".providers[${index}].default_model"]`);
      expect(model?.hasAttribute("data-figure")).toBe(true);
      expect(element.querySelector(`[data-path=".providers[${index}].key_var"]`)).toBeTruthy();
      const figure = mark.querySelector("[data-figure]")?.textContent;
      states.push(`${names[index]}: configured: ${figure} (${`.providers[${index}].configured`})`);
    }
    const text = section(PROVIDERS_TITLE).textContent ?? "";
    expect(text).not.toMatch(/\b(connected|active)\b/i);
    log.push("card states (configured, verbatim):", ...states, "");
  });

  it("writes an unknown default_model and a keyless key_var with their reasons", async () => {
    answersWith(recordedRoutes());
    await renderModels();

    const ollama = card("ollama");
    expect(within(ollama).getAllByText("unknown").length).toBe(2);
    expect(ollama.textContent).toContain("needs no key");
    expect(ollama.textContent).toContain("no default is known");
    const groq = card("groq");
    expect(groq.textContent).toContain("GROQ_API_KEY");
    expect(groq.textContent).toContain("openai/gpt-oss-20b");
  });

  // ---- AC 4: unconfigured open, configured collapsed behind "rotate key", keyless none --------

  it("opens the form on an unconfigured card and collapses it behind rotate key on a configured one", async () => {
    answersWith(recordedRoutes());
    await renderModels();

    const groq = card("groq");
    const field = within(groq).getByLabelText(credentialLabel("groq")) as HTMLInputElement;
    expect(field.type).toBe("password");
    expect(within(groq).queryByRole("button", { name: ROTATE_KEY })).toBeNull();

    const openrouter = card("openrouter");
    expect(within(openrouter).queryByLabelText(credentialLabel("openrouter"))).toBeNull();
    const rotate = within(openrouter).getByRole("button", { name: ROTATE_KEY });
    expect(rotate.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(rotate);
    expect(rotate.getAttribute("aria-expanded")).toBe("true");
    expect(within(openrouter).getByLabelText(credentialLabel("openrouter"))).toBeTruthy();
    fireEvent.click(rotate);
    expect(within(openrouter).queryByLabelText(credentialLabel("openrouter"))).toBeNull();

    for (const keyless of ["fake", "ollama"]) {
      const element = card(keyless);
      expect(within(element).queryByRole("textbox")).toBeNull();
      expect(element.querySelector("input")).toBeNull();
      expect(within(element).queryByRole("button", { name: ROTATE_KEY })).toBeNull();
    }
    log.push(
      "unconfigured (groq): form open, type=password, no rotate key",
      "configured (openrouter): form collapsed behind a 'rotate key' text action",
      "keyless (fake, ollama): no form, no rotate key",
      "",
    );
  });

  it("reads the providers again after a key is saved, and the typed value is gone", async () => {
    const spy = answersWith(
      recordedRoutes({ [CONTROL_PROVIDERS_ROUTE]: { status: 200, text: SAVED_BODY } }),
    );
    await renderModels();

    const groq = card("groq");
    fireEvent.change(within(groq).getByLabelText(credentialLabel("groq")), {
      target: { value: TYPED },
    });
    await act(async () => {
      fireEvent.click(within(groq).getByRole("button", { name: SAVE }));
    });
    await settle();

    const calls = spy.mock.calls as [string, RequestInit | undefined][];
    const posts = calls.filter(([, init]) => init?.method === "POST");
    expect(posts.map(([url]) => url)).toEqual([CONTROL_PROVIDERS_ROUTE]);
    expect(JSON.parse(String(posts[0]?.[1]?.body))).toEqual({ provider: "groq", credential: TYPED });
    expect(calls.filter(([url]) => url === PROVIDERS).length).toBe(2);
    for (const [url] of calls) expect(url).not.toContain(TYPED);
    expect(document.body.textContent).not.toContain(TYPED);
  });

  it("keeps the providers as they were when the save is refused", async () => {
    const spy = answersWith(
      recordedRoutes({ [CONTROL_PROVIDERS_ROUTE]: { status: 400, text: REFUSED_BODY } }),
    );
    await renderModels();

    const groq = card("groq");
    fireEvent.change(within(groq).getByLabelText(credentialLabel("groq")), {
      target: { value: TYPED },
    });
    await act(async () => {
      fireEvent.click(within(groq).getByRole("button", { name: SAVE }));
    });
    await settle();
    expect(spy.mock.calls.filter(([url]) => url === PROVIDERS).length).toBe(1);
    expect(card("groq").textContent).toContain("CredentialRefused");
  });

  // ---- AC 5: the control census ---------------------------------------------------------------

  it("has 0 pin, star, make-default or model-switch controls; the only write is the key form", async () => {
    answersWith(recordedRoutes());
    await renderModels();
    const main = screen.getByRole("main");

    const buttons = within(main).getAllByRole("button");
    const names = buttons.map((button) => button.textContent?.replace(/\s+/g, " ").trim() ?? "");
    const forbidden = /\b(pin|unpin|star|favou?rite|default|switch|select|use|choose)\b/i;
    expect(names.filter((name) => forbidden.test(name))).toEqual([]);
    // A `Value` figure's own accessible name is its `unknown, <null reason>` announcement (e.g.
    // "no default is known for this provider"), a description, never a pin/star/switch action; a
    // figure only ever opens `ProvenanceDrawer`, so it is exempt from this action-name scan.
    for (const button of buttons.filter((candidate) => !candidate.hasAttribute("data-figure"))) {
      expect(button.getAttribute("aria-label") ?? "").not.toMatch(forbidden);
      expect(button.getAttribute("title") ?? "").not.toMatch(forbidden);
    }
    // The one search box (ticket 202) is a combobox once its datalist is attached; nothing else is.
    expect(within(main).queryAllByRole("combobox")).toEqual([within(main).getByLabelText(SEARCH)]);
    expect(within(main).queryAllByRole("listbox")).toEqual([]);
    expect(within(main).queryAllByRole("radio")).toEqual([]);
    expect(within(main).queryAllByRole("checkbox")).toEqual([]);
    expect(within(main).queryAllByRole("switch")).toEqual([]);
    expect(main.querySelectorAll("select")).toHaveLength(0);

    const searches = [...main.querySelectorAll("input[type='search']")];
    expect(searches).toHaveLength(1);
    const inputs = [...main.querySelectorAll("input")].filter((input) => input.type !== "search");
    expect(inputs.every((input) => input.type === "password")).toBe(true);
    const submits = buttons.filter((button) => button.getAttribute("type") === "submit");
    expect(submits.every((button) => button.textContent === SAVE)).toBe(true);
    expect(submits.length).toBe(inputs.length);

    const census = [...new Set(names)].sort();
    log.push(
      "control census in <main>:",
      `  buttons: ${buttons.length} (${census.join(" | ")})`,
      `  pin/star/default/switch matches: ${names.filter((name) => forbidden.test(name)).length}`,
      `  inputs: ${inputs.length}, all type=password; submit buttons: ${submits.length}, all "${SAVE}"`,
      "",
    );
  });

  // ---- The prices panel -------------------------------------------------------------------------

  it("offers a search and the served model names, and shows no price until one is typed", async () => {
    answersWith(recordedRoutes());
    await renderModels();
    const panel = section(PRICES_TITLE);
    const input = within(panel).getByLabelText(SEARCH) as HTMLInputElement;
    expect(input.type).toBe("search");
    const list = panel.querySelector("datalist");
    expect(list?.id).toBe(input.getAttribute("list"));
    const options = Array.from(list?.querySelectorAll("option") ?? []);
    expect(options.map((option) => option.getAttribute("value"))).toEqual(
      sentPrices.map((price) => price.model),
    );
    expect(panel.textContent).toContain(HINT);
    expect(panel.querySelectorAll("li")).toHaveLength(0);
    expect(panel.querySelectorAll("table")).toHaveLength(0);
  });

  it("shows the matching rows as cards in served order, a missing cache rate unknown with its reason", async () => {
    answersWith(recordedRoutes());
    await renderModels();
    const panel = section(PRICES_TITLE);
    typeQuery(panel, "GPT");
    const cards = Array.from(panel.querySelectorAll("li"));
    expect(cards).toHaveLength(1);
    const text = cards[0]?.textContent ?? "";
    for (const value of Object.values(sentPrices[2] ?? {})) expect(text).toContain(value as string);
    expect(panel.textContent).not.toContain(HINT);

    typeQuery(panel, "deepseek");
    const missing = panel.querySelector("li")?.textContent ?? "";
    expect(missing).toContain("unknown");
    expect(missing).toContain("the catalog published no cache rate");
    expect(missing).toContain("https://openrouter.ai/api/v1/models");
  });

  it("says no stored price matches, and brings the hint back when cleared", async () => {
    answersWith(recordedRoutes());
    await renderModels();
    const panel = section(PRICES_TITLE);
    typeQuery(panel, "zzz");
    expect(panel.textContent).toContain("No stored price matches.");
    expect(panel.querySelectorAll("li")).toHaveLength(0);
    typeQuery(panel, "");
    expect(panel.textContent).toContain(HINT);
    expect(panel.textContent).not.toContain("No stored price matches.");
  });

  it("after a query matching every row, one card per served price in served order", async () => {
    answersWith(recordedRoutes());
    await renderModels();
    const panel = section(PRICES_TITLE);
    typeQuery(panel, "/");
    const cards = Array.from(panel.querySelectorAll("li"));
    expect(cards.map((card) => card.textContent)).toEqual(
      sentPrices.map((price) => expect.stringContaining(price.model as string) as unknown as string),
    );
    expect(panel.textContent).toContain("the catalog published no cache rate");
  });

  it("says zero stored prices and names the command that stores some", async () => {
    answersWith(recordedRoutes({ [PRICES]: { status: noPrices.code, text: noPrices.text } }));
    await renderModels();
    const panel = section(PRICES_TITLE);
    expect(panel.textContent).toContain("No prices recorded yet.");
    expect(panel.textContent).toContain("harnessed prices refresh");
    expect(within(panel).queryByLabelText(SEARCH)).toBeNull();
    expect(section(PROVIDERS_TITLE).querySelectorAll("[data-testid='provider-card']")).toHaveLength(7);
  });

  it("quotes the gateway's error and names an unreachable gateway, one panel at a time", async () => {
    answersWith(
      recordedRoutes({
        [PROVIDERS]: { status: storageError.code, text: storageError.text },
        [PRICES]: "rejects",
      }),
    );
    await renderModels();
    expect(section(PROVIDERS_TITLE).textContent).toContain("StorageError");
    expect(section(PROVIDERS_TITLE).querySelectorAll("[data-testid='provider-card']")).toHaveLength(0);
    expect(section(PRICES_TITLE).textContent).toContain("ECONNREFUSED");
  });
});

// ---- AC 6: the sidebar ------------------------------------------------------------------------

/** Which area of design.md's v4 order a nav entry belongs to. Compare is Evals', Facts Memory's. */
const AREA: Record<string, string> = {
  "/": "Overview",
  "/console": "Console",
  "/evals": "Evals",
  "/memory": "Memory",
  "/traces": "Traces",
  "/models": "Models",
};

const V4_ORDER = [
  "Overview",
  "Console",
  "Evals",
  "Memory",
  "Traces",
  "Models",
];

describe("the sidebar", () => {
  it("is one list in the v4 order, Models directly beneath Traces, no pinned footer", async () => {
    answersWith(recordedRoutes());
    await renderModels();
    const nav = screen.getByRole("navigation", { name: "Pages" });
    expect(nav.querySelectorAll("ul")).toHaveLength(1);
    expect(nav.querySelector("footer")).toBeNull();

    const links = within(nav).getAllByRole("link");
    const hrefs = links.map((link) => link.getAttribute("href") ?? "");
    expect(hrefs).toEqual(PAGES.map((page) => page.href));
    const areas = hrefs.map((href) => AREA[href]);
    expect(areas.every((area) => area !== undefined)).toBe(true);
    expect(areas.filter((area, index) => area !== areas[index - 1])).toEqual(V4_ORDER);
    expect(links.at(-1)?.textContent).toBe("Models");
    expect(links.at(-2)?.textContent).toBe("Traces");

    log.push(
      "sidebar order (one <ul>, no footer group):",
      ...links.map((link) => `  ${link.textContent} -> ${link.getAttribute("href")}`),
      "",
    );
  });
});

/** The control app's refusal: the route's own shape, as ticket 146's suite captured it. */
const REFUSED_BODY =
  '{"error":"CredentialRefused","message":"provider \'groq\' refused the credential: https://api.groq.com/openai/v1 answered 401. Nothing was written."}';

// ---- The states each panel can reach: every one a real answer, none of it padding ---------------

/** Decision 24's 404 for a route the gateway does not serve: an older gateway, before FR-89. */
const unknownRoute = recorded("notfound.route");

/** Six minutes: past the five minute mark `lib/clock.ts` names stale. */
const SIX_MINUTES = 360_000;

describe("the Models panels' states", () => {
  it("opens a card figure's source, the providers route and its --json twin, and closes back", async () => {
    answersWith(recordedRoutes());
    await renderModels();
    const figure = card("groq").querySelector<HTMLElement>("[data-figure]");
    expect(figure).not.toBeNull();
    fireEvent.click(figure as HTMLElement);
    const drawer = screen.getByRole("dialog");
    expect(within(drawer).getByText("Providers: this machine")).toBeTruthy();
    expect(drawer.textContent).toContain("harnessed providers list --json");
    expect(drawer.textContent).not.toContain(TYPED);
    expect(drawer.textContent).not.toContain(PROVIDERS);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();

    typeQuery(section(PRICES_TITLE), "/");
    const price = section(PRICES_TITLE).querySelector<HTMLElement>("[data-figure]");
    expect(price).not.toBeNull();
    fireEvent.click(price as HTMLElement);
    const priceDrawer = screen.getByRole("dialog");
    expect(within(priceDrawer).getByText("Prices: this machine")).toBeTruthy();
    expect(priceDrawer.textContent).toContain("harnessed prices list --json");
    expect(priceDrawer.textContent).not.toContain(PRICES);
    fireEvent.click(within(priceDrawer).getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("goes stale past five minutes keeping its cards, and a Refetch reads it fresh", async () => {
    vi.useFakeTimers({ toFake: ["Date", "setInterval", "clearInterval"] });
    try {
      const spy = answersWith(recordedRoutes());
      await renderModels();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(SIX_MINUTES);
      });
      const providersPanel = section(PROVIDERS_TITLE);
      expect(within(providersPanel).getByTestId("stale-badge").getAttribute("data-warn")).toBe("true");
      expect(within(providersPanel).getAllByTestId("provider-card")).toHaveLength(7);
      expect(within(section(PRICES_TITLE)).getByTestId("stale-badge").getAttribute("data-warn")).toBe(
        "true",
      );

      await act(async () => {
        fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
      });
      await settle();
      expect(
        within(section(PROVIDERS_TITLE)).getByTestId("stale-badge").getAttribute("data-warn"),
      ).toBe("false");
      expect(spy.mock.calls.filter(([url]) => url === PROVIDERS).length).toBe(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it("names a gateway with no providers route as absent, and an unreadable 2xx as unreadable", async () => {
    answersWith(
      recordedRoutes({
        [PROVIDERS]: { status: unknownRoute.code, text: unknownRoute.text },
        [PRICES]: { status: 200, text: "<html>an intermediary's page</html>" },
      }),
    );
    await renderModels();
    const providersPanel = section(PROVIDERS_TITLE);
    expect(providersPanel.textContent).toContain("SurfaceNotFound");
    expect(within(providersPanel).queryAllByTestId("provider-card")).toHaveLength(0);
    expect(section(PRICES_TITLE).textContent).toContain("an intermediary's page");
  });

  it("reports a refusal with no message as the error's own name", async () => {
    const spy = vi.fn(async (url: string) => {
      if (url === HEALTH) return answer({ status: health.code, text: health.text });
      if (url === PROVIDERS) throw new TypeError("");
      throw "the browser refused the request";
    });
    vi.stubGlobal("fetch", spy);
    await renderModels();
    expect(section(PROVIDERS_TITLE).textContent).toContain("TypeError");
    expect(section(PRICES_TITLE).textContent).toContain("the browser refused the request");
  });

  it("says a save the control app never answered, and reads nothing again", async () => {
    const spy = answersWith(recordedRoutes({ [CONTROL_PROVIDERS_ROUTE]: "rejects" }));
    await renderModels();
    const groq = card("groq");
    fireEvent.change(within(groq).getByLabelText(credentialLabel("groq")), {
      target: { value: TYPED },
    });
    await act(async () => {
      fireEvent.click(within(groq).getByRole("button", { name: SAVE }));
    });
    await settle();
    expect(card("groq").textContent).toContain("ECONNREFUSED");
    expect(card("groq").textContent).not.toContain(CONTROL_PROVIDERS_ROUTE);
    expect(spy.mock.calls.filter(([url]) => url === PROVIDERS).length).toBe(1);
    expect((within(card("groq")).getByLabelText(credentialLabel("groq")) as HTMLInputElement).value).toBe(
      "",
    );
  });

  it("quotes a control-app answer that is not its contract, a 2xx or a 5xx alike", async () => {
    answersWith(
      recordedRoutes({
        [CONTROL_PROVIDERS_ROUTE]: [
          { status: 200, text: "not json" },
          { status: 502, text: "Bad Gateway" },
        ],
      }),
    );
    await renderModels();
    for (const expected of ["not json", "Bad Gateway"]) {
      const groq = card("groq");
      fireEvent.change(within(groq).getByLabelText(credentialLabel("groq")), {
        target: { value: TYPED },
      });
      await act(async () => {
        fireEvent.click(within(groq).getByRole("button", { name: SAVE }));
      });
      await settle();
      expect(card("groq").textContent).toContain(expected);
    }
  });

  it("opens a price's drawer from the keyboard, traps Tab and copies its curl line", async () => {
    const written: string[] = [];
    vi.stubGlobal("navigator", {
      clipboard: { writeText: (line: string) => Promise.resolve(void written.push(line)) },
    });
    vi.stubGlobal("matchMedia", (query: string) => ({ matches: true, media: query }));
    answersWith(recordedRoutes());
    await renderModels();
    typeQuery(section(PRICES_TITLE), "/");
    const figure = section(PRICES_TITLE).querySelector<HTMLElement>("[data-figure]");
    expect(figure).not.toBeNull();
    fireEvent.keyDown(figure as HTMLElement, { key: "Enter" });

    const drawer = screen.getByRole("dialog");
    expect(drawer.dataset.motion).toBe("instant");
    expect(within(drawer).getByText("Prices: this machine")).toBeTruthy();
    const controls = within(drawer).getAllByRole("button");
    const first = controls[0] as HTMLElement;
    const last = controls.at(-1) as HTMLElement;
    expect(document.activeElement).toBe(first);
    fireEvent.keyDown(drawer, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(last);
    fireEvent.keyDown(drawer, { key: "Tab" });
    expect(document.activeElement).toBe(first);
    fireEvent.keyDown(drawer, { key: "Tab" });
    fireEvent.keyDown(drawer, { key: "a" });

    await act(async () => {
      fireEvent.click(within(drawer).getByRole("button", { name: "Copy" }));
    });
    expect(written).toEqual([expect.stringContaining("harnessed prices list --json")]);
    // No endpoint string anywhere in the dialog (FR-129).
    expect(drawer.textContent).not.toContain(PRICES);
    expect(drawer.textContent?.toLowerCase()).not.toContain("curl");
    const disclosures = within(drawer).queryAllByRole("button", { name: "Show fields" });
    if (disclosures.length > 0) {
      fireEvent.click(disclosures[0] as HTMLElement);
      expect(within(drawer).getAllByRole("button", { name: "Hide fields" }).length).toBeGreaterThan(0);
    }
    fireEvent.click(within(drawer).getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(figure);
  });

  it("names the gateway's state in the pill whatever the health route answers", async () => {
    for (const [route, word] of [
      [{ status: storageError.code, text: storageError.text }, `gateway error ${storageError.code}`],
      ["rejects", "gateway unreachable"],
    ] as const) {
      answersWith(recordedRoutes({ [HEALTH]: route }));
      await renderModels();
      const pill = screen.getByTestId("gateway-status-pill") as HTMLButtonElement;
      expect(pill.textContent).toContain(word);
      if (!pill.disabled) {
        fireEvent.click(pill);
        expect(screen.getByRole("dialog").textContent).toContain("StorageError");
        fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Close" }));
        expect(document.activeElement).toBe(pill);
      }
      cleanup();
      vi.unstubAllGlobals();
    }
  });

  it("opens the sidebar as a menu at 400px and closes it again", async () => {
    answersWith(recordedRoutes());
    await renderModels();
    const menu = screen.getByRole("button", { name: "Menu" });
    fireEvent.click(menu);
    expect(menu.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByRole("navigation", { name: "Pages" }).dataset.open).toBe("true");
    fireEvent.click(menu);
    expect(menu.getAttribute("aria-expanded")).toBe("false");
  });
});

// ---- FR-121 (ticket 186): the router card, GET /v1/router --------------------------------------

function routerCard(): HTMLElement {
  return within(section(ROUTER_TITLE)).getByTestId("router-card");
}

const NEVER_PROBED_TEXT = "never probed: run the live doctor rung";

describe("the Router card", () => {
  it("reads unknown with the never-probed reason and the live rung's copy line before any probe", async () => {
    const writeText = vi.fn(async () => {});
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    answersWith(recordedRoutes());
    const { container } = await renderModels();

    const card = routerCard();
    const badge = within(card).getByTestId("router-probe-badge");
    expect(badge.textContent).toBe("never probed");
    expect(badge.getAttribute("data-word")).toBe("never probed");
    expect([...ROUTER_WORDS]).toEqual(["probed", "failed", "never probed"]);
    expect(within(card).getAllByText(NEVER_PROBED_TEXT).length).toBeGreaterThan(0);
    expect(within(card).getAllByText("unknown").length).toBeGreaterThan(0);

    const command = within(card).getByTestId("router-live-command");
    expect(command.textContent).toBe(LIVE_RUNG_COMMAND);
    fireEvent.click(within(card).getByRole("button", { name: ROUTER_COPY_LABEL }));
    expect(writeText).toHaveBeenCalledWith(LIVE_RUNG_COMMAND);

    // The floors and the needs-memory threshold are never a probe's own field, so they still show.
    expect(card.textContent).toContain("50%");

    expect(figuresWithoutSource(container)).toEqual([]);
    expect(
      divergences(
        renderedFigures(section(ROUTER_TITLE)).filter((f) => f.path !== ".routing"),
        new Map([[ROUTER, router.text]]),
      ),
    ).toEqual([]);
    registryLog.push(`GET ${ROUTER} (never probed): badge=${badge.textContent}`);
  });

  it("ticket 317: says routing is off and which key turns it on, from the served fields", async () => {
    const off = recorded("router.routing-off");
    answersWith(recordedRoutes({ [ROUTER]: { status: off.code, text: off.text } }));
    await renderModels();

    const card = routerCard();
    const routing = within(card).getByTestId("router-routing");
    expect(routing.textContent).toContain("Routing is off");
    expect(card.textContent).toContain("routing is off: set OPENROUTER_API_KEY to turn it on");
  });

  it("ticket 317: routing on reads 'Routing is on' and no reason", async () => {
    answersWith(recordedRoutes());
    await renderModels();
    const routing = within(routerCard()).getByTestId("router-routing");
    expect(routing.textContent).toContain("Routing is on");
    expect(routerCard().textContent).not.toContain("routing is off");
  });

  it("draws Router once as a heading (ticket 305, I2)", async () => {
    answersWith(recordedRoutes());
    await renderModels();
    const headings = screen.getAllByRole("heading").filter((heading) => heading.textContent === "Router");
    expect(headings).toHaveLength(1);
    expect(routerCard().querySelector("h3")).toBeNull();
  });

  it("renders the fail state on a failed probe, the model and detail still named", async () => {
    answersWith(recordedRoutes({ [ROUTER]: { status: routerFailed.code, text: routerFailed.text } }));
    await renderModels();

    const card = routerCard();
    const badge = within(card).getByTestId("router-probe-badge");
    expect(badge.textContent).toBe("failed");
    expect(card.textContent).toContain("typesafe/jev-1.13-20260917");
    expect(card.textContent).toContain("fail: DecisionTimeout");
    expect(within(card).getByText("no cost reported for this probe")).toBeTruthy();
    registryLog.push(`GET ${ROUTER} (failed): badge=${badge.textContent}`);
  });

  it("shows the model, the probe badge with latency and cost, and every floor as served", async () => {
    answersWith(recordedRoutes({ [ROUTER]: { status: routerProbed.code, text: routerProbed.text } }));
    const { container } = await renderModels();

    const card = routerCard();
    const badge = within(card).getByTestId("router-probe-badge");
    expect(badge.textContent).toBe("probed");
    expect(card.textContent).toContain("typesafe/jev-1.13-20260917");
    expect(card.textContent).toContain("0.842");
    expect(card.textContent).toContain("0.0031");
    const floors = (JSON.parse(routerProbed.text) as { floors: { floor_percent: string }[] }).floors;
    for (const floor of floors) expect(card.textContent).toContain(floor.floor_percent);

    expect(figuresWithoutSource(container)).toEqual([]);
    expect(
      divergences(
        // latency_seconds and probed_at read in words (ticket 303, 317); their exact text is the tooltip's.
        renderedFigures(section(ROUTER_TITLE)).filter((f) => !/^\.(latency_seconds|probed_at|routing)$/.test(f.path)),
        new Map([[ROUTER, routerProbed.text]]),
      ),
    ).toEqual([]);
    expect(card.textContent).toContain("0.842 seconds");
    expect(card.textContent).not.toMatch(/\d{4}-\d{2}-\d{2}T/);
    registryLog.push(`GET ${ROUTER} (probed): badge=${badge.textContent}`);
  });

  it("is the page's #router anchor, the one the diagram's delegation box links to", async () => {
    answersWith(recordedRoutes());
    await renderModels();
    const anchor = document.getElementById("router");
    expect(anchor).toBeTruthy();
    expect(anchor?.contains(routerCard())).toBe(true);
  });
});

// ---- FR-121 (ticket 186): the registry section, GET /v1/agents, RegistryCard --------------------

function registryCard(agent: string): HTMLElement {
  return within(section(AGENTS_TITLE)).getByRole("region", { name: agent });
}

type AgentRow = { agent: string };

describe("the Registry section", () => {
  it("renders one card per row, each reading live, stale, failed or unproven as a word", async () => {
    answersWith(recordedRoutes());
    const { container } = await renderModels();

    const rows = (JSON.parse(agents.text) as { agents: AgentRow[] }).agents.map((row) => row.agent);
    const cards = within(section(AGENTS_TITLE)).getAllByTestId("registry-card");
    expect(cards.map((element) => element.querySelector("h3")?.textContent)).toEqual(rows);

    // The recording holds one agent per word, in the registry's order (the recorder's seed).
    const words = cards.map((element) => within(element).getByTestId("probe-badge").textContent);
    expect(words).toEqual(["live", "stale", "failed", "unproven"]);
    expect([...REGISTRY_WORDS]).toEqual(["live", "stale", "failed", "unproven"]);
    for (const element of cards) {
      const badge = within(element).getByTestId("probe-badge");
      // A word, drawn as text; the ground colour only repeats it.
      expect(badge.textContent?.trim()).not.toBe("");
      expect(badge.getAttribute("data-word")).toBe(badge.textContent);
      expect(badge.getAttribute("aria-hidden")).toBeNull();
    }

    // `kind: "acp"` renders its human word, "Live agent", on every row (`lib/human-words.ts`); the
    // exact wire word still rides the figure's tooltip, the same convention `key_kind` uses.
    for (const [index, element] of cards.entries()) {
      const kind = element.querySelector('[data-path$=".kind"]');
      expect(kind?.textContent).toBe(humanWord("kind", "acp"));
      expect(kind?.getAttribute("title")).toContain("acp");
      expect(within(element).getByText("not metered")).toBeTruthy();
      registryLog.push(`  row ${index} (${rows[index]}): kind=${kind?.textContent}, billing=not metered`);
    }

    expect(figuresWithoutSource(container)).toEqual([]);
    const panel = section(AGENTS_TITLE);
    const humanized = new Set(cards.map((_, index) => `.agents[${index}].kind`));
    const literal = renderedFigures(panel).filter((figure) => !humanized.has(figure.path));
    expect(divergences(literal, new Map([[AGENTS, agents.text]]))).toEqual([]);
    registryLog.push(
      `GET ${AGENTS} (recorded): ${rows.length} rows`,
      ...cards.map((_, index) => `  ${rows[index]}: ${words[index]}`),
      "",
    );
  });

  it("writes an unproven agent's null fields as unknown with the reason, and warns on empty use_for", async () => {
    const writeText = vi.fn(async () => {});
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    answersWith(recordedRoutes());
    await renderModels();
    const unproven = registryCard("delta");
    // `RegistryCard` reads no field of `delta`'s row as null but `billing` (`status`,
    // `fingerprint_matches`, `protocol_version`, `tested_at` and `detail` are `AgentCard`'s own
    // fields, not `RegistryCard`'s - design.md's field table).
    expect(within(unproven).getAllByText("unknown").length).toBe(1);
    expect(unproven.textContent).toContain("not metered");

    // Every recorded row's `use_for` is empty (decision 33's fixture), so every card warns.
    for (const name of ["alpha", "bravo", "charlie", "delta"]) {
      const element = registryCard(name);
      expect(within(element).getByText(NO_USE_FOR)).toBeTruthy();
      const command = within(element).getByTestId("doctor-command");
      expect(command.textContent).toBe("harnessed doctor");
    }
    fireEvent.click(within(registryCard("alpha")).getAllByRole("button", { name: REGISTRY_COPY_LABEL })[0] as HTMLElement);
    expect(writeText).toHaveBeenCalledWith("harnessed doctor");
  });

  it("has 0 test buttons and exactly one copyable harnessed agents test line per card", async () => {
    const writeText = vi.fn(async () => {});
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    answersWith(recordedRoutes());
    await renderModels();

    const panel = section(AGENTS_TITLE);
    const cards = within(panel).getAllByTestId("registry-card");
    const lines: string[] = [];
    for (const element of cards) {
      const name = element.querySelector("h3")?.textContent ?? "";
      const buttons = within(element).getAllByRole("button");
      const names = buttons.map((button) => button.textContent ?? "");
      expect(names.filter((label) => /\b(test|probe|run|retry)\b/i.test(label))).toEqual([]);
      // One copy button for the empty-`use_for` warning, one for the agent-test line.
      expect(names.filter((label) => label === REGISTRY_COPY_LABEL)).toHaveLength(2);
      const copyLines = within(element).getAllByTestId("agent-test-command");
      expect(copyLines).toHaveLength(1);
      expect(copyLines[0]?.textContent).toBe(`harnessed agents test ${name}`);
      expect(element.querySelectorAll("form, input")).toHaveLength(0);
      // The agent-test line's own copy button is the last of the card's copy buttons: the
      // empty-`use_for` warning's copy button (present on every recorded row) comes first.
      const copyButtons = within(element).getAllByRole("button", { name: REGISTRY_COPY_LABEL });
      fireEvent.click(copyButtons.at(-1) as HTMLElement);
      lines.push(`  ${copyLines[0]?.textContent}  (buttons: ${names.join(" | ")})`);
    }
    expect(writeText.mock.calls.map((call: unknown[]) => call[0])).toEqual(
      cards.map((element) => `harnessed agents test ${element.querySelector("h3")?.textContent}`),
    );
    // A `Value` figure's own accessible name can be an "unknown, <null reason>" announcement that
    // happens to mention `harnessed agents test` in prose (e.g. an unproven agent's fields); it is
    // never itself a test/probe/run action, so it is exempt from this action-name scan.
    expect(
      within(panel)
        .queryAllByRole("button", { name: /\btest\b/i })
        .filter((button) => !button.hasAttribute("data-figure")),
    ).toEqual([]);
    registryLog.push("copy lines, one per card, 0 test buttons:", ...lines, "");
  });

  it("says no agent CLIs are configured when the route answers zero rows, never a blank grid", async () => {
    answersWith(recordedRoutes({ [AGENTS]: { status: noAgents.code, text: noAgents.text } }));
    await renderModels();
    const panel = section(AGENTS_TITLE);
    expect(within(panel).queryAllByTestId("registry-card")).toHaveLength(0);
    expect(panel.textContent).toContain("no agent CLIs configured: see `harnessed agents add`");
    expect(panel.querySelector("ul")).toBeNull();
    registryLog.push(`GET ${AGENTS} = ${noAgents.text}`, `  renders: ${panel.textContent}`, "");
  });

  it("quotes the gateway's error and names an unreachable gateway on the agents panel", async () => {
    answersWith(recordedRoutes({ [AGENTS]: { status: storageError.code, text: storageError.text } }));
    await renderModels();
    expect(section(AGENTS_TITLE).textContent).toContain("StorageError");
    cleanup();
    answersWith(recordedRoutes({ [AGENTS]: "rejects" }));
    await renderModels();
    expect(section(AGENTS_TITLE).textContent).toContain("ECONNREFUSED");
    expect(within(section(AGENTS_TITLE)).queryAllByTestId("registry-card")).toHaveLength(0);
  });
});

// ---- FR-129 (ticket 186): the Models page closes with SystemSection -----------------------------

describe("SystemSection", () => {
  it("closes the page collapsed, aria-expanded false, and opens to hold the health panel", async () => {
    const spy = answersWith(recordedRoutes());
    await renderModels();

    const models = [PROVIDERS_TITLE, PRICES_TITLE, ROUTER_TITLE, AGENTS_TITLE].map((title) =>
      section(title),
    );
    const trigger = screen.getByRole("button", { name: SYSTEM_TITLE });
    expect(trigger.getAttribute("aria-expanded")).toBe("false");
    expect(models.at(-1)?.compareDocumentPosition(trigger)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    // Collapsed: `HealthPanel` is not mounted, so it has read `/v1/health` 0 times of its own -
    // whatever count the pill's own poll already holds is `GatewayStatusPill`'s, not this panel's.
    expect(screen.queryByRole("heading", { name: "Gateway health" })).toBeNull();
    const beforeOpen = spy.mock.calls.filter(([url]) => url === HEALTH).length;

    fireEvent.click(trigger);
    await settle();
    expect(trigger.getAttribute("aria-expanded")).toBe("true");
    const healthPanel = screen.getByRole("heading", { name: "Gateway health" }).closest("section") as HTMLElement;
    expect(within(healthPanel).getByTestId("schema-version-gloss")).toBeTruthy();
    expect(within(healthPanel).getByText("37")).toBeTruthy();
    expect(spy.mock.calls.filter(([url]) => url === HEALTH).length).toBe(beforeOpen + 1);

    fireEvent.click(trigger);
    expect(trigger.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByRole("heading", { name: "Gateway health" })).toBeNull();
  });
});
