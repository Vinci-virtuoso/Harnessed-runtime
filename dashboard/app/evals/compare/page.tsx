"use client";

/** `/evals/compare` moved: its content is on `/evals`, and this stub forwards there (ticket 207). */
import { Forward } from "../../../components/Forward";

const PARAMS = { before: "before", after: "after" } as const;

export default function CompareForward() {
  return <Forward to="/evals" page="Evals" params={PARAMS} />;
}
