"use client";

/**
 * The frame every page is rendered into (FR-54; design.md inventory, `experience.md` route map).
 *
 * It holds the three landmarks - `nav`, `header`, `main` - and a skip link to `main` that is first
 * in tab order, so the keyboard path `experience.md` documents starts where it says it starts.
 * The header holds the one polling component in the dashboard, `GatewayStatusPill`, and the menu
 * button that replaces the sidebar at 400px.
 *
 * **It holds no gateway figure.** The shell is rendered from `app/layout.tsx`, a static file
 * (decision 44); the only gateway value anywhere near it is the origin every panel under it, and
 * the pill, reads the gateway at - the relative `/v1`, on the origin that served this page
 * (decision 43, MUST rule 22). It is provided here once so no panel spells it for itself.
 *
 * **The nav links to pages, never to ids.** Every entry below is a static page of the route map;
 * the pages whose path holds a bracketed segment are reached from a field of a response or from
 * an `IdLookup`, because a nav link to one would have to build an id (MUST rule 25).
 */
import Link from "next/link";
import { usePathname } from "next/navigation";
import { type ReactNode, useCallback, useMemo, useState } from "react";

import { GATEWAY_ORIGIN } from "../lib/fetch-panel";
import { surfaceOf } from "../lib/surface";

import { GatewayStatusPill } from "./GatewayStatusPill";
import { GatewayOriginContext, RefreshContext } from "./PanelFrame";
import { ThemeToggle } from "./ThemeToggle";
import styles from "./AppShell.module.css";

/** One nav entry: a page of `ux/experience.md`'s Next.js route map, and what it is called. */
export type Page = { href: string; label: string };

/**
 * Every static page of that map, in the order the information architecture puts them: design.md's
 * "Navigation order (v4)", one list with no pinned footer group. `Models` sits directly beneath
 * `Traces`, because a settings page floated to the bottom reads as a different kind of place.
 */
export const PAGES: readonly Page[] = [
  { href: "/", label: "Overview" },
  { href: "/console", label: "Console" },
  { href: "/evals", label: "Evals" },
  { href: "/memory", label: "Memory" },
  { href: "/traces", label: "Traces" },
  { href: "/models", label: "Models" },
];

/**
 * Each page's 16px line icon (design.md "Nav icon geometry", `NavIcon`): the spine's inner markup,
 * keyed by `href`. Inline SVG, no icon package (decision 68), nothing fetched.
 */
const GLYPHS: Record<string, ReactNode> = {
  "/": (
    <>
      <rect x="2.5" y="2.5" width="4.5" height="4.5" rx="1" />
      <rect x="9" y="2.5" width="4.5" height="4.5" rx="1" />
      <rect x="2.5" y="9" width="4.5" height="4.5" rx="1" />
      <rect x="9" y="9" width="4.5" height="4.5" rx="1" />
    </>
  ),
  "/console": (
    <>
      <path d="M3.5 4.5L6.5 8l-3 3.5" />
      <path d="M8.5 11.5H13" />
    </>
  ),
  "/evals": (
    <>
      <rect x="2.5" y="2.5" width="11" height="11" rx="2" />
      <path d="M5.5 8.2l1.8 1.8 3.3-3.6" />
    </>
  ),
  "/memory": (
    <>
      <ellipse cx="8" cy="4" rx="5" ry="1.8" />
      <path d="M3 4v8c0 1 2.2 1.8 5 1.8s5-.8 5-1.8V4" />
      <path d="M3 8c0 1 2.2 1.8 5 1.8s5-.8 5-1.8" />
    </>
  ),
  "/traces": <path d="M2 8h2.5l1.5-4 3 8 1.5-4H14" />,
  "/models": (
    <>
      <circle cx="8" cy="8" r="2" />
      <path d="M8 2v2M8 12v2M2 8h2M12 8h2M4 4l1.4 1.4M10.6 10.6L12 12M12 4l-1.4 1.4M5.4 10.6L4 12" />
    </>
  ),
};

/** The current page's item: `/` only on `/` exactly, every other page on itself and its sub-routes. */
function isCurrent(href: string, pathname: string | null): boolean {
  if (pathname === null) return false;
  return href === "/" ? pathname === "/" : pathname === href || pathname.startsWith(`${href}/`);
}

/** The nav's id, so the menu button can say what it controls and the skip link can be first. */
const NAV_ID = "app-nav";

type Props = {
  children: ReactNode;
};

export function AppShell({ children }: Props) {
  // Below 400px the sidebar is a menu; above it the button is not rendered by the stylesheet at
  // all, so this flag only ever describes a sidebar the operator asked to see.
  const [open, setOpen] = useState(false);
  // The dashboard's one Refresh (ticket 211): a fresh token per press, read by every panel and
  // every bare read under the open page.
  const [token, setToken] = useState<object>({});
  const refetch = useCallback(() => setToken({}), []);
  const refresh = useMemo(() => ({ token, refetch }), [token, refetch]);
  // A page listed in `SOLID_PAGES` renders solid: the attribute is on the root `<div>`, in the export.
  const pathname = usePathname();
  const surface = surfaceOf(pathname);

  return (
    <GatewayOriginContext.Provider value={GATEWAY_ORIGIN}>
      <RefreshContext.Provider value={refresh}>
      <div className={styles.shell} data-surface={surface}>
        <a className={styles.skip} href="#main">
          Skip to main content
        </a>
        <nav
          id={NAV_ID}
          className={styles.nav}
          data-open={open ? "true" : "false"}
          aria-label="Pages"
        >
          <div className={styles.brand}>
            <span className={styles.mark} aria-hidden="true">
              <svg
                width="12"
                height="12"
                viewBox="0 0 16 16"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.5"
                strokeLinecap="round"
                strokeLinejoin="round"
                focusable="false"
              >
                <path d="M4.5 3v10M11.5 3v10M4.5 8h7" />
              </svg>
            </span>
            <span className={styles.wordmark}>Harnessed</span>
          </div>
          <ul className={styles.list}>
            {PAGES.map((page) => (
              <li key={page.href}>
                <Link
                  className={styles.link}
                  href={page.href}
                  aria-current={isCurrent(page.href, pathname) ? "page" : undefined}
                >
                  <svg
                    className={styles.icon}
                    width="16"
                    height="16"
                    viewBox="0 0 16 16"
                    fill="none"
                    strokeWidth="1.5"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    aria-hidden="true"
                    focusable="false"
                  >
                    {GLYPHS[page.href]}
                  </svg>
                  {page.label}
                </Link>
              </li>
            ))}
          </ul>
          <div className={styles.spacer} />
          <ThemeToggle />
        </nav>
        <header className={styles.header}>
          <button
            type="button"
            className={styles.menu}
            aria-controls={NAV_ID}
            aria-expanded={open}
            onClick={() => setOpen(!open)}
          >
            Menu
          </button>
          <GatewayStatusPill />
          <button type="button" className={styles.refresh} onClick={refetch}>
            Refresh
          </button>
        </header>
        <main id="main" className={styles.main}>
          {children}
        </main>
      </div>
      </RefreshContext.Provider>
    </GatewayOriginContext.Provider>
  );
}
