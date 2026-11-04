import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import type { NextConfig } from 'next';

const here = dirname(fileURLToPath(import.meta.url));
// Backend origin for the same-origin /api/v1 proxy (resolved when the server starts/builds).
const apiUrl = (process.env.AI3D_API_URL ?? 'http://127.0.0.1:8000').replace(/\/+$/, '');

const nextConfig: NextConfig = {
  reactStrictMode: true,
  output: 'standalone',
  outputFileTracingRoot: resolve(here, '../..'),
  transpilePackages: [
    '@ai3d/ui',
    '@ai3d/studio',
    '@ai3d/api-client',
    '@ai3d/shared-types',
    '@ai3d/utilities',
    '@ai3d/viewer',
    '@ai3d/editor',
  ],
  poweredByHeader: false,
  // Next blocks dev-only assets for unknown origins; allow the loopback IP too.
  allowedDevOrigins: ['127.0.0.1'],
  experimental: {
    // The /api/v1 proxy gives up after 30 s by default. Measuring photo overlap and
    // uploading large photo sets can take longer on a busy machine.
    proxyTimeout: 10 * 60 * 1000,
    // Next cuts proxied request bodies at 10 MB by default, which silently broke
    // uploads of large photos and models. The backend enforces its own limits
    // (50 MB per photo, 500 MB per model); this only has to stay above them.
    proxyClientMaxBodySize: '520mb',
  },
  async rewrites() {
    return [{ source: '/api/v1/:path*', destination: `${apiUrl}/api/v1/:path*` }];
  },
};

export default nextConfig;
