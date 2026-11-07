import base from './playwright.config';

// The same browser tests against a production build of the web app (`next build` +
// `next start`), as users get it. Development mode keeps extra references for its
// tools (e.g. old components), so memory checks are only meaningful here:
//   pnpm exec playwright test -c playwright.production.config.ts e2e/viewer-memory.spec.ts
process.env.AI3D_E2E_PRODUCTION = '1';

const [backend, web] = base.webServer as NonNullable<typeof base.webServer> & object[];
const port = new URL(String(web!.url)).port;
const api = String((web!.env as Record<string, string>).AI3D_API_URL);

export default {
  ...base,
  webServer: [
    backend!,
    {
      ...web!,
      // The /api/v1 proxy target is fixed when the app is built.
      command: `AI3D_API_URL=${api} pnpm --filter @ai3d/web build && pnpm --filter @ai3d/web exec next start --port ${port}`,
      timeout: 600_000,
    },
  ],
};
