import type { Metadata } from "next";
import { Suspense, type ReactNode } from "react";
import { GeistMono } from "geist/font/mono";
import { GeistSans } from "geist/font/sans";

import { AppShell } from "../components/AppShell";

import "./tokens.css";

export const metadata: Metadata = {
  title: "Harnessed",
  description: "A read-only view of the Harnessed gateway.",
};

// A static file (decision 44): nothing is read when a page is served, so nothing here is dynamic.
// The boundary is where a page's `useSearchParams` hands rendering to the browser (decision 45):
// the export holds the shell, and the page fills in once the URL can be read.
export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" className={`${GeistSans.variable} ${GeistMono.variable}`}>
      <head>
        {/* Decision 85: a plain, synchronous script, so the stored theme is on `html` before first paint. */}
        {/* eslint-disable-next-line @next/next/no-sync-scripts */}
        <script src="/theme-init.js"></script>
      </head>
      <body>
        <AppShell>
          <Suspense fallback={null}>{children}</Suspense>
        </AppShell>
      </body>
    </html>
  );
}
