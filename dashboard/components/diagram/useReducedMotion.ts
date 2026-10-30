import { useSyncExternalStore } from "react";

/** The media query `design.md`'s motion rules answer to. */
const REDUCE = "(prefers-reduced-motion: reduce)";

/** Whether the operator asked for no motion. A browser with no `matchMedia` is treated as asking. */
function reducedMotion(): boolean {
  const media = globalThis.matchMedia;
  if (typeof media !== "function") return true;
  return media(REDUCE).matches;
}

/** Follow the preference while the page is open; a `matchMedia` with no listener API is read once. */
function onMotionChange(changed: () => void): () => void {
  const media = globalThis.matchMedia;
  if (typeof media !== "function") return () => {};
  const query = media(REDUCE);
  query.addEventListener?.("change", changed);
  return () => query.removeEventListener?.("change", changed);
}

/** `true` when the operator asked for reduced motion (and on the server, where nothing animates). */
export function useReducedMotion(): boolean {
  return useSyncExternalStore(onMotionChange, reducedMotion, () => true);
}
