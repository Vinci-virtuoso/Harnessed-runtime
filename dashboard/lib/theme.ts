/**
 * The theme choice, remembered in this browser only (FR-157, decision 85).
 *
 * This is the one module allowed `localStorage` (decision 32's allowlist). The choice is a per-viewer
 * convenience: never served, never in the ledger. `public/theme-init.js` reads the same key before
 * first paint; it is a static file and cannot import this one, so the key is spelled in both.
 */
export type Theme = "light" | "dark";

export const THEME_KEY = "harnessed.theme";

/** Store the choice. A private window or blocked site data throws; the page still switches. */
export function remember(choice: Theme): void {
  try {
    window.localStorage.setItem(THEME_KEY, choice);
  } catch {
    // Not remembered: the choice holds for the page's lifetime, which is all a private window offers.
  }
}
