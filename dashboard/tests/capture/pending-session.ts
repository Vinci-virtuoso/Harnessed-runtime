/** Refuse ambiguous captures rather than photographing the wrong session. */
export function pendingSession(payload: unknown): string {
  if (!payload || typeof payload !== "object" || !("permissions" in payload)
      || !Array.isArray(payload.permissions)) {
    throw new Error("pending permissions response is malformed");
  }
  const sessions = new Set<string>();
  for (const item of payload.permissions) {
    if (!item || typeof item.session_id !== "string" || !item.session_id.trim()) {
      throw new Error("pending permission has no session id");
    }
    sessions.add(item.session_id);
  }
  if (sessions.size !== 1) {
    throw new Error(`expected one pending session, found ${sessions.size}`);
  }
  return [...sessions][0];
}
