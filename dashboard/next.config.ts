import type { NextConfig } from "next";

/**
 * The gateway `next dev` talks to, and the only origin the dashboard tree spells anywhere. It is
 * read by the development rewrite alone: an exported build ignores `rewrites()` (decision 44),
 * the production branch below declares none, and `tests/unit/export-config.test.ts` pins both.
 */
const DEV_GATEWAY_ORIGIN = "http://127.0.0.1:8756";

const nextConfig: NextConfig = {
  // The dashboard is a static export served by `harnessed dashboard` (FR-63, decision 44).
  output: "export",
  // No gzip setting here on purpose: the passed-through text/event-stream it once guarded no
  // longer exists - uvicorn serves the stream directly (decision 43).
  poweredByHeader: false,
  reactStrictMode: true,
  async rewrites() {
    // Development only: `next dev` has no gateway mounted beside it, so `/v1/*` and
    // `/control/v1/*` are forwarded to the operator's `harnessed serve`. In production there is
    // no rewrite at all - decision 29 rejected one, and export mode could not honour it anyway.
    if (process.env.NODE_ENV === "development") {
      return [
        { source: "/v1/:path*", destination: `${DEV_GATEWAY_ORIGIN}/v1/:path*` },
        { source: "/control/v1/:path*", destination: `${DEV_GATEWAY_ORIGIN}/control/v1/:path*` },
      ];
    }
    return [];
  },
};

export default nextConfig;
