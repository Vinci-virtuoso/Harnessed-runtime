"use client";

/** `/memory/facts` moved: its content is on `/memory`, and this stub forwards there (ticket 208). */
import { Forward } from "../../../components/Forward";

const PARAMS = { q: "q", id: "id" } as const;

export default function MemoryForward() {
  return <Forward to="/memory" page="Memory" params={PARAMS} />;
}
