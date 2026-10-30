"use client";

/** `/improve` moved: its content is on `/evals`, and this stub forwards there (ticket 207). */
import { Forward } from "../../components/Forward";

const PARAMS = { limit: "limit", id: "cycle" } as const;

export default function ImproveForward() {
  return <Forward to="/evals" page="Evals" params={PARAMS} />;
}
