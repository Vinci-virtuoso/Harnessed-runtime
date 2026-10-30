/**
 * The offline rule in JavaScript (decision 30, MUST rules 12 and 18). Both halves are proved by
 * doing the thing: a real loopback server accepts a connection, and a non-loopback attempt on the
 * very same port is refused, so the predicate is shown to be the host and never the port.
 */
import dns from "node:dns";
import http from "node:http";
import net from "node:net";
import tls from "node:tls";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { isLoopbackHost } from "../setup/loopback";

const REFUSED = /socket guard/;
const REMOTE = "203.0.113.7"; // TEST-NET-3: documentation space, never a real host

let server: http.Server;
let port = 0;

beforeAll(async () => {
  server = http.createServer((_request, response) => response.end("ok"));
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  port = (server.address() as net.AddressInfo).port;
});

afterAll(async () => {
  server.closeAllConnections();
  await new Promise<void>((resolve) => server.close(() => resolve()));
});

describe("the loopback half", () => {
  it("lets a socket reach a real loopback server", async () => {
    const socket = net.connect({ host: "127.0.0.1", port });
    await new Promise<void>((resolve, reject) => {
      socket.once("connect", resolve);
      socket.once("error", reject);
    });
    socket.destroy();
  });

  it("lets fetch read from a real loopback server", async () => {
    const response = await fetch(`http://127.0.0.1:${port}/`);
    expect(await response.text()).toBe("ok");
  });

  it("lets dns.lookup resolve a literal loopback address", async () => {
    const address = await new Promise<string>((resolve, reject) =>
      dns.lookup("127.0.0.1", (error, found) => (error ? reject(error) : resolve(found))),
    );
    expect(address).toBe("127.0.0.1");
  });
});

describe("the refusal half", () => {
  it("refuses a socket to a remote host on the loopback server's own port", () => {
    expect(() => net.connect({ host: REMOTE, port })).toThrow(REFUSED);
    expect(() => new net.Socket().connect(port, REMOTE)).toThrow(REFUSED);
  });

  it("refuses a socket that names no host, because the default is a name", () => {
    expect(() => net.connect(port)).toThrow(REFUSED);
  });

  it("refuses tls.connect to a remote host", () => {
    expect(() => tls.connect({ host: REMOTE, port: 443 })).toThrow(REFUSED);
    expect(() => tls.connect(443, "example.com")).toThrow(REFUSED);
  });

  it("refuses dns.lookup of a remote name", () => {
    expect(() => dns.lookup("example.com", () => undefined)).toThrow(REFUSED);
  });

  it("refuses fetch to a remote origin, whatever the input shape", async () => {
    await expect(fetch(`http://${REMOTE}:${port}/`)).rejects.toThrow(REFUSED);
    await expect(fetch(new URL("https://example.com/"))).rejects.toThrow(REFUSED);
    await expect(fetch(new Request("https://example.com/"))).rejects.toThrow(REFUSED);
  });
});

describe("isLoopbackHost", () => {
  it.each(["127.0.0.1", "127.8.9.10", "::1", "[::1]", "::ffff:127.0.0.1"])(
    "admits the literal loopback address %s",
    (host) => expect(isLoopbackHost(host)).toBe(true),
  );

  it.each(["localhost", "10.0.0.1", "128.0.0.1", "example.com", "::2", "", undefined])(
    "refuses %s",
    (host) => expect(isLoopbackHost(host)).toBe(false),
  );
});
