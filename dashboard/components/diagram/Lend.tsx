import { useCallback, useEffect, useState } from "react";

import type { RawResponse } from "../../lib/fetch-panel";
import type { Leaf } from "../../lib/wire";
import { usePanel } from "../PanelFrame";

/** One label a panel lends the diagram: its own `Leaf`, and the response that `Leaf` was read from. */
type Lent = { leaf: Leaf; response: RawResponse };

/** Hand a label to the diagram, or (`undefined`) take it back. */
export type Lender = (key: string, lent: Lent | undefined) => void;

/**
 * The labels the panels have lent, held for exactly as long as each panel renders them. Only the
 * `Leaf` and its response travel (decision 42): never its text, never a copy kept past its panel.
 */
export function useLentLabels(): {
  labels: Record<string, Leaf>;
  sources: Record<string, RawResponse>;
  lend: Lender;
} {
  const [lent, setLent] = useState<Record<string, Lent>>({});
  const lend = useCallback<Lender>((key, next) => {
    setLent((held) => {
      const others = Object.fromEntries(Object.entries(held).filter(([name]) => name !== key));
      return next === undefined ? others : { ...others, [key]: next };
    });
  }, []);
  const entries = Object.entries(lent);
  return {
    labels: Object.fromEntries(entries.map(([key, { leaf }]) => [key, leaf])),
    sources: Object.fromEntries(entries.map(([, { leaf, response }]) => [leaf.route, response])),
    lend,
  };
}

/**
 * A panel's `Leaf`, lent to the diagram under `name` while this panel renders it. It renders
 * nothing itself: the tile beside it is the panel's figure, and the diagram's label is the same
 * `Leaf` under the same response.
 */
export function Lend({ name, leaf, to }: { name: string; leaf: Leaf; to: Lender }) {
  const { response } = usePanel();
  useEffect(() => {
    to(name, { leaf, response });
    return () => to(name, undefined);
  }, [name, leaf, response, to]);
  return null;
}
