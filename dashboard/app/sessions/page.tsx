"use client";

/** `/sessions` moved: its content is on `/traces`, and this stub forwards there (ticket 206). */
import { Forward } from "../../components/Forward";

const PARAMS = { limit: "limit", key: "key", session: "session" } as const;

export default function SessionsForward() {
  return <Forward to="/traces" page="Traces" params={PARAMS} />;
}
