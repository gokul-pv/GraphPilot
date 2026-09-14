import type { NextConfig } from 'next'

/**
 * Two build targets, mutually exclusive on purpose.
 *
 * `next dev` needs a rewrite proxy: the console talks to the Python API on
 * :8110, and neither that server nor the gateway behind it installs CORS
 * middleware, so a browser on :3000 cannot reach either directly.
 *
 * `BUILD_TARGET=export pnpm build` emits a static site into `out/`, which
 * agent/api.py serves itself — same origin, so no proxy is needed or possible.
 * Static export does not support rewrites at all, hence the either/or rather
 * than one config carrying both.
 *
 * If the dev proxy ever buffers the SSE stream (the one plausible failure mode
 * here — everything else is plain JSON), start the API with
 * `AGENT_API_CORS_ORIGINS=http://localhost:3000` and set
 * `NEXT_PUBLIC_API_BASE=http://127.0.0.1:8110` to take the proxy out of the
 * path entirely.
 */
const isExport = process.env.BUILD_TARGET === 'export'

const nextConfig: NextConfig = isExport
  ? { output: 'export' }
  : {
      async rewrites() {
        return [
          {
            source: '/api/:path*',
            destination: 'http://127.0.0.1:8110/api/:path*',
          },
        ]
      },
    }

export default nextConfig
