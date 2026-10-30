// @vitest-environment jsdom
/**
 * `components/ProviderCard.tsx`'s `CredentialField` (FR-88, ticket 146): the form and the request.
 * The credential leaves in a JSON body to `/control/v1/providers` and nowhere else, the field is
 * cleared on any answer (200, refusal, none), and no browser storage is touched. The card's
 * rendering of provider state is ticket 147's suite (`models.test.tsx`), so this file mounts the
 * card over one unconfigured row, whose field is open; `tests/test_control_providers.py` holds the
 * server-side sweeps.
 *
 * The control app's 200 and 400 have no recorder (ticket 139's learning); the bodies below are the
 * route's own shapes, and the refusal message is the one `tests/test_control_providers.py` captured.
 */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ProviderCard,
  SAVE,
  SAVED_ANNOUNCEMENT,
  SAVING,
  SAVING_ANNOUNCEMENT,
  credentialLabel,
} from "../../components/ProviderCard";
import { PROVIDERS_ROUTE } from "../../lib/control-client";
import type { Leaf } from "../../lib/wire";

const PROVIDER = "groq";

/** The one leaf this file builds: row 2 of a `/v1/providers` body, as `lib/wire.ts` hands it out. */
function leaf(kind: Leaf["kind"], text: string, field: string): Leaf {
  return { kind, text, path: `.providers[2].${field}`, route: "/v1/providers" };
}

/** An unconfigured keyed row, so the card holds its field open (ticket 147 renders the rest). */
const UNCONFIGURED = {
  name: leaf("string", PROVIDER, "name"),
  configured: leaf("boolean", "false", "configured"),
  keyVar: leaf("string", "GROQ_API_KEY", "key_var"),
  defaultModel: leaf("string", "openai/gpt-oss-20b", "default_model"),
};
const TYPED = "not-a-real-value-31415926535897932384";

const OK_BODY =
  '{"credential_live":true,"credential_var":"GROQ_API_KEY","env_file":".env","provider":"groq","rungs":[],"unchanged":false}';
const REFUSED_BODY =
  '{"error":"CredentialRefused","message":"provider \'groq\' refused the credential: https://api.groq.com/openai/v1 answered 401. Nothing was written."}';

type Pending = { resolve: (response: Response) => void; reject: (cause: unknown) => void };

/** A `fetch` the test answers by hand, so the `saving` state can be looked at mid-flight. */
function heldFetch(): { spy: ReturnType<typeof vi.fn>; pending: Pending[] } {
  const pending: Pending[] = [];
  const spy = vi.fn(
    () => new Promise<Response>((resolve, reject) => pending.push({ resolve, reject })),
  );
  vi.stubGlobal("fetch", spy);
  return { spy, pending };
}

function answer(status: number, text: string): Response {
  return { status, ok: status >= 200 && status < 300, text: async () => text } as Response;
}

function field(): HTMLInputElement {
  return screen.getByLabelText(credentialLabel(PROVIDER)) as HTMLInputElement;
}

function save(): HTMLButtonElement {
  return screen.getByRole("button", { name: SAVE }) as HTMLButtonElement;
}

/** Every storage API a key could be left in, spied so a single write fails the test. */
function storageWrites(): ReturnType<typeof vi.fn>[] {
  return [
    vi.spyOn(Storage.prototype, "setItem"),
    vi.spyOn(document, "cookie", "set"),
  ] as unknown as ReturnType<typeof vi.fn>[];
}

async function submit(): Promise<void> {
  fireEvent.change(field(), { target: { value: TYPED } });
  await act(async () => {
    fireEvent.click(save());
  });
}

let writes: ReturnType<typeof vi.fn>[];

beforeEach(() => {
  writes = storageWrites();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("the CredentialField", () => {
  it("is a password field in an autocomplete=off form, labelled in visible text", () => {
    render(<ProviderCard {...UNCONFIGURED} />);
    expect(field().type).toBe("password");
    expect(field().getAttribute("autocomplete")).toBe("off");
    expect(field().closest("form")?.getAttribute("autocomplete")).toBe("off");
    expect(field().getAttribute("placeholder")).toBeNull();
    expect(screen.getByRole("heading", { name: PROVIDER })).toBeTruthy();
    expect(save().disabled).toBe(true);
  });

  it("posts the credential as a JSON body, never in the URL, and clears on a 200", async () => {
    const { spy, pending } = heldFetch();
    const answered = vi.fn();
    render(<ProviderCard {...UNCONFIGURED} onAnswered={answered} />);

    await submit();
    expect(screen.getByRole("button", { name: SAVING })).toBeTruthy();
    expect(screen.getByRole("status").textContent).toBe(SAVING_ANNOUNCEMENT);
    expect(field().disabled).toBe(true);

    const [url, init] = spy.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(PROVIDERS_ROUTE);
    expect(url).not.toContain(TYPED);
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ provider: PROVIDER, credential: TYPED });

    await act(async () => pending[0]?.resolve(answer(200, OK_BODY)));
    expect(field().value).toBe("");
    expect(screen.getByRole("status").textContent).toBe(SAVED_ANNOUNCEMENT);
    expect(answered).toHaveBeenCalledWith(expect.objectContaining({ state: "ok" }));
    expect(document.body.textContent).not.toContain(TYPED);
    for (const write of writes) expect(write).not.toHaveBeenCalled();
  });

  it("clears on a refusal and quotes the control app's status word", async () => {
    const { pending } = heldFetch();
    render(<ProviderCard {...UNCONFIGURED} />);

    await submit();
    await act(async () => pending[0]?.resolve(answer(400, REFUSED_BODY)));

    expect(field().value).toBe("");
    expect(field().disabled).toBe(false);
    expect(document.body.textContent).toContain("CredentialRefused");
    expect(document.body.textContent).toContain("answered 401");
    expect(document.body.textContent).toContain("control app");
    expect(document.body.textContent).not.toContain(TYPED);
    for (const write of writes) expect(write).not.toHaveBeenCalled();
  });

  it("clears when the control app never answers, and says so", async () => {
    const { pending } = heldFetch();
    render(<ProviderCard {...UNCONFIGURED} />);

    await submit();
    await act(async () => pending[0]?.reject(new TypeError("Failed to fetch")));

    expect(field().value).toBe("");
    expect(document.body.textContent).toContain("Failed to fetch");
    expect(document.body.textContent).not.toContain(TYPED);
  });

  it("sends nothing for an empty field", async () => {
    const { spy } = heldFetch();
    render(<ProviderCard {...UNCONFIGURED} />);
    await act(async () => {
      fireEvent.submit(field().closest("form") as HTMLFormElement);
    });
    expect(spy).not.toHaveBeenCalled();
  });
});
