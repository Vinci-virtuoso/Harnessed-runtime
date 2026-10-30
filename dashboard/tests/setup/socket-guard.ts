/**
 * The socket guard, in JavaScript (decision 30). A Vitest `setupFiles` entry, so it is in place
 * before any test module loads. It refuses every connection whose host is not a literal loopback
 * address, through the four doors a Node process has to the network: `net.Socket#connect` (which
 * `net.connect`, `http` and `tls` all reach), `tls.connect`, `dns.lookup` and `globalThis.fetch`.
 *
 * The predicate is on the host and never on the port (MUST rule 18): a port allowlist admits a
 * remote host that happens to listen on that port. A name, `localhost` included, is refused,
 * because only a resolver can say where a name points.
 */
import dns from "node:dns";
import net from "node:net";
import { syncBuiltinESMExports } from "node:module";
import tls from "node:tls";

import { isLoopbackHost } from "./loopback";

export class SocketGuardError extends Error {
  constructor(door: string, host: unknown) {
    super(`socket guard: ${door} to ${String(host)} refused; unit tests stay on loopback`);
    this.name = "SocketGuardError";
  }
}

type ConnectArgs = unknown[];

/** The host a `connect(...)` call names, from any of Node's argument shapes; null for a pipe. */
function hostOf(args: ConnectArgs): unknown {
  let first = args[0];
  if (Array.isArray(first)) first = first[0]; // the internal normalized-args form
  if (first !== null && typeof first === "object") {
    const options = first as { host?: unknown; path?: unknown };
    if (options.path !== undefined && options.host === undefined) return null;
    return options.host ?? "localhost";
  }
  if (typeof first === "string" && !/^\d+$/.test(first)) return null; // an IPC path
  return typeof args[1] === "string" ? args[1] : "localhost";
}

function refuseUnlessLoopback(door: string, host: unknown): void {
  if (host !== null && !isLoopbackHost(host)) throw new SocketGuardError(door, host);
}

const originalSocketConnect = net.Socket.prototype.connect;
net.Socket.prototype.connect = function guardedConnect(this: net.Socket, ...args: ConnectArgs) {
  refuseUnlessLoopback("net.Socket#connect", hostOf(args));
  return (originalSocketConnect as (...a: ConnectArgs) => net.Socket).apply(this, args);
} as typeof net.Socket.prototype.connect;

const originalTlsConnect = tls.connect;
tls.connect = function guardedTlsConnect(...args: ConnectArgs) {
  refuseUnlessLoopback("tls.connect", hostOf(args));
  return (originalTlsConnect as (...a: ConnectArgs) => tls.TLSSocket)(...args);
} as typeof tls.connect;

const originalLookup = dns.lookup;
dns.lookup = function guardedLookup(hostname: unknown, ...rest: unknown[]) {
  refuseUnlessLoopback("dns.lookup", hostname);
  return (originalLookup as (...a: unknown[]) => void)(hostname, ...rest);
} as typeof dns.lookup;

syncBuiltinESMExports();

const originalFetch = globalThis.fetch;
globalThis.fetch = function guardedFetch(input: RequestInfo | URL, init?: RequestInit) {
  const href = input instanceof Request ? input.url : String(input);
  const base = globalThis.location?.href;
  try {
    refuseUnlessLoopback("fetch", new URL(href, base).hostname);
  } catch (error) {
    return Promise.reject(error);
  }
  return originalFetch(input, init);
};
