/**
 * `lib/control-client.ts`: the one door that writes (FR-65; decisions 43, 49; MUST rules 22,
 * 23, 25, 26). Every request it makes is a `POST` to a `/control/v1/...` route on this page's
 * origin with a JSON body; a rejected `fetch` is unreachable and every non-2xx is the control
 * app's own body, verbatim, exactly as `fetch-panel.ts` treats a read.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  PROVIDERS_ROUTE,
  RUNS_ROUTE,
  TASKS_ROUTE,
  answerPermission,
  configureProvider,
  messagesRoute,
  permissionRoute,
  postControl,
  queueTask,
  sendMessage,
  startRun,
} from "../../lib/control-client";

type Answer = { status: number; text: string };

function answer({ status, text }: Answer): Response {
  return {
    status,
    ok: status >= 200 && status < 300,
    text: async () => text,
  } as unknown as Response;
}

function answersWith(next: Answer): ReturnType<typeof vi.fn> {
  const spy = vi.fn(async () => answer(next));
  vi.stubGlobal("fetch", spy);
  return spy;
}

function requestOf(spy: ReturnType<typeof vi.fn>): { url: string; init: RequestInit } {
  const [url, init] = spy.mock.calls[0] as [string, RequestInit];
  return { url, init };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the route table", () => {
  it("names only /control/v1 routes and places a session id verbatim", () => {
    expect(RUNS_ROUTE).toBe("/control/v1/runs");
    expect(PROVIDERS_ROUTE).toBe("/control/v1/providers");
    expect(messagesRoute("run_01")).toBe("/control/v1/runs/run_01/messages");
    const hostile = "a/b?c&d=e f";
    const route = messagesRoute(hostile);
    const head = "/control/v1/runs/";
    expect(route.startsWith(head)).toBe(true);
    expect(route.endsWith("/messages")).toBe(true);
    expect(decodeURIComponent(route.slice(head.length, route.lastIndexOf("/")))).toBe(hostile);
  });
});

describe("startRun", () => {
  it("POSTs one string under the key prompt, uncached, and reads the 202 through parseWire", async () => {
    const spy = answersWith({
      status: 202,
      text: '{"session_id":"run_7","stream":"/v1/sessions/run_7/stream","run":"/v1/runs/run_7"}',
    });
    const result = await startRun("say hello");
    const { url, init } = requestOf(spy);
    expect(url).toBe(RUNS_ROUTE);
    expect(init.method).toBe("POST");
    expect(init.cache).toBe("no-store");
    expect(JSON.parse(init.body as string)).toEqual({ prompt: "say hello" });
    expect(result.state).toBe("ok");
    if (result.state !== "ok") return;
    expect(result.response.url).toBe(RUNS_ROUTE);
    expect(result.response.status).toBe("202");
    expect(result.wire.at(".session_id").text).toBe("run_7");
  });
});

describe("sendMessage", () => {
  it("posts the same one-string body to the session's messages route", async () => {
    const spy = answersWith({ status: 202, text: '{"session_id":"run_7"}' });
    await sendMessage("run_7", "and again");
    const { url, init } = requestOf(spy);
    expect(url).toBe("/control/v1/runs/run_7/messages");
    expect(JSON.parse(init.body as string)).toEqual({ prompt: "and again" });
  });
});

describe("configureProvider", () => {
  it("posts the provider and credential to /control/v1/providers", async () => {
    const spy = answersWith({ status: 200, text: '{"provider":"anthropic"}' });
    const result = await configureProvider("anthropic", "sk-seeded");
    const { url, init } = requestOf(spy);
    expect(url).toBe(PROVIDERS_ROUTE);
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({
      provider: "anthropic",
      credential: "sk-seeded",
    });
    expect(result.state).toBe("ok");
  });
});

describe("ticket 253: answering a permission and queueing a task", () => {
  it("names the permission route with the id placed verbatim", () => {
    expect(TASKS_ROUTE).toBe("/control/v1/tasks");
    expect(permissionRoute("perm_01")).toBe("/control/v1/permissions/perm_01");
    expect(permissionRoute("a/b")).toBe("/control/v1/permissions/a%2Fb");
  });

  it("POSTs exactly one {answer} to the request's own route", async () => {
    const spy = answersWith({ status: 200, text: '{"permission_id":"perm_01","state":"approved"}' });
    const result = await answerPermission("perm_01", "approve");
    const { url, init } = requestOf(spy);
    expect(url).toBe("/control/v1/permissions/perm_01");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ answer: "approve" });
    expect(spy).toHaveBeenCalledTimes(1);
    expect(result.state).toBe("ok");
  });

  it("keeps a 409's whole body for the caller, quoting error and message", async () => {
    answersWith({
      status: 409,
      text: '{"error":"PermissionAnswered","message":"answered","answer":"approve","answered_via":"cli","answered_at":"2026-01-01T12:15:00Z"}',
    });
    const result = await answerPermission("perm_01", "deny");
    expect(result.state).toBe("error");
    if (result.state !== "error") return;
    expect(result.error).toBe("PermissionAnswered");
    expect(result.response.status).toBe("409");
    expect(JSON.parse(result.response.text).answered_via).toBe("cli");
  });

  it("POSTs one {text} to the tasks route and reads the 201 through parseWire", async () => {
    const spy = answersWith({
      status: 201,
      text: '{"task_id":"task_01","state":"queued","budget_iterations":"20","budget_seconds":"600","budget_dollars":"0.50","budget_source":"default","queued_via":"console"}',
    });
    const result = await queueTask("tidy the notes");
    const { url, init } = requestOf(spy);
    expect(url).toBe(TASKS_ROUTE);
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ text: "tidy the notes" });
    expect(result.state).toBe("ok");
    if (result.state !== "ok") return;
    expect(result.wire.at(".budget_dollars").text).toBe("0.50");
  });
});

describe("the states", () => {
  it("is unreachable only when the browser fetch rejects, quoting the cause", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    );
    const result = await startRun("x");
    expect(result).toEqual({ state: "unreachable", url: RUNS_ROUTE, cause: "Failed to fetch" });
  });

  it("quotes the control app's own error body on a non-2xx", async () => {
    answersWith({ status: 409, text: '{"error":"RunInFlight","message":"run_7 is running"}' });
    const result = await startRun("x");
    expect(result.state).toBe("error");
    if (result.state !== "error") return;
    expect(result.error).toBe("RunInFlight");
    expect(result.message).toBe("run_7 is running");
    expect(result.response.status).toBe("409");
  });

  it("names an unreadable body as itself rather than rejecting", async () => {
    answersWith({ status: 502, text: "<html>bad gateway</html>" });
    const refused = await postControl(RUNS_ROUTE, { prompt: "x" });
    expect(refused.state).toBe("error");
    if (refused.state !== "error") return;
    expect(refused.error).toBe("UnreadableBody");
    expect(refused.message).toBe("<html>bad gateway</html>");

    answersWith({ status: 200, text: "not json" });
    const accepted = await postControl(RUNS_ROUTE, { prompt: "x" });
    expect(accepted.state).toBe("error");
    if (accepted.state !== "error") return;
    expect(accepted.error).toBe("UnreadableBody");
  });
});
