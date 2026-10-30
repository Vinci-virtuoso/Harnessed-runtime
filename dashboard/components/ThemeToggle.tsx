"use client";

/**
 * The sidebar's last item: one button that switches the theme (FR-157, decision 85; design.md's
 * `ThemeToggle` row).
 *
 * `public/theme-init.js` names the action from `data-label-light` / `data-label-dark` before hydration (FR-174).
 * The showing theme is read from `html[data-theme]`, else from `prefers-color-scheme`, and followed
 * as either changes. The static export renders dark; the browser's own value replaces it after
 * hydration. A press sets the attribute and hands the choice to `lib/theme.ts`. It reads nothing
 * from the gateway, makes no request, and a change is neither announced nor animated: the swap
 * holds `data-theme-swap` on `html` for the one style pass it causes, which `tokens.css` answers
 * with `transition: none`, so the controls' own hover transitions never cross-fade a theme.
 */
import { useSyncExternalStore } from "react";

import { remember, type Theme } from "../lib/theme";

import styles from "./ThemeToggle.module.css";

const LIGHT_QUERY = "(prefers-color-scheme: light)";

function subscribe(notify: () => void): () => void {
  const found = typeof window.matchMedia === "function" ? window.matchMedia(LIGHT_QUERY) : null;
  // A stubbed `matchMedia` may be only a `{ matches }`; the real one always has the listener methods.
  const media = typeof found?.addEventListener === "function" ? found : null;
  media?.addEventListener("change", notify);
  const watch = new MutationObserver(notify);
  watch.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  return () => {
    media?.removeEventListener("change", notify);
    watch.disconnect();
  };
}

function showing(): Theme {
  const chosen = document.documentElement.dataset.theme;
  if (chosen === "light" || chosen === "dark") return chosen;
  const light = typeof window.matchMedia === "function" && window.matchMedia(LIGHT_QUERY).matches;
  return light ? "light" : "dark";
}

const serverTheme = (): Theme => "dark";

export function ThemeToggle() {
  const theme = useSyncExternalStore(subscribe, showing, serverTheme);
  const next: Theme = theme === "dark" ? "light" : "dark";
  const action = `Switch to ${next} theme`;

  const press = () => {
    // Controls transition their own hover and press (FR-162), but a theme change has no cross-fade
    // (FR-157): hold every transition off for the one style pass the swap causes, then release it.
    const root = document.documentElement;
    root.dataset.themeSwap = "";
    root.dataset.theme = next;
    void root.offsetHeight;
    delete root.dataset.themeSwap;
    remember(next);
  };

  return (
    <button type="button" className={styles.toggle} aria-label={action}
      title={action}
      data-label-light="Switch to dark theme"
      data-label-dark="Switch to light theme"
      suppressHydrationWarning
      onClick={press}
    >
      <svg
        width="16"
        height="16"
        viewBox="0 0 16 16"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
        focusable="false"
        data-icon={theme === "dark" ? "sun" : "moon"}
      >
        {theme === "dark" ? (
          <>
            <circle cx="8" cy="8" r="3" />
            <path d="M8 1.5v1.5M8 13v1.5M1.5 8H3M13 8h1.5M3.4 3.4l1 1M11.6 11.6l1 1M3.4 12.6l1-1M11.6 4.4l1-1" />
          </>
        ) : (
          <path d="M13.5 9.5A5.5 5.5 0 1 1 6.5 2.5a4.5 4.5 0 0 0 7 7z" />
        )}
      </svg>
    </button>
  );
}
