/**
 * The socket guard's one predicate, kept apart from the guard so a test can import it without
 * installing the guard: the refusal tests then pass only if `setupFiles` installed it.
 */
import { isIP } from "node:net";

/** True for a literal IPv4 127/8 or IPv6 ::1 address (bracketed or v4-mapped), false otherwise. */
export function isLoopbackHost(host: unknown): boolean {
  if (typeof host !== "string") return false;
  const bare = host.startsWith("[") && host.endsWith("]") ? host.slice(1, -1) : host;
  const mapped = bare.toLowerCase().startsWith("::ffff:") ? bare.slice(7) : bare;
  if (isIP(mapped) === 4) return mapped.split(".")[0] === "127";
  return isIP(bare) === 6 && bare === "::1";
}

