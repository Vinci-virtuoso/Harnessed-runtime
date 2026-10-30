"use client";

/** `/runs` moved: its content is on `/traces`, and this stub forwards there (ticket 206). */
import { Forward } from "../../components/Forward";

const PARAMS = { limit: "limit", key: "key", session: "session" } as const;

export default function RunsForward() {
  return <Forward to="/traces" page="Traces" params={PARAMS} />;
}
