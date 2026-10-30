"use client";

/** `/memory/episodes` moved: its content is on `/memory`, and this stub forwards there (ticket 208). */
import { Forward } from "../../../components/Forward";

const PARAMS = { session: "session" } as const;

export default function MemoryForward() {
  return <Forward to="/memory" page="Memory" params={PARAMS} />;
}
