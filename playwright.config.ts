import { defineConfig, devices } from '@playwright/test';

// The suite starts its own backend (fresh data dir) and web dev server on
// dedicated ports, so it never touches your development data.
const BACKEND_PORT = 8765;
const WEB_PORT = 3765;
const DATA_DIR = 'test-results/e2e-data';

export default defineConfig({
  testDir: './e2e',
  // Suites with their own backends: playwright.desktop.config.ts, playwright.workflows.config.ts.
  testIgnore: ['desktop-shell/**', 'workflows/**'],
  // `next dev` compiles each page and API route on its first request, which
  // takes 5–20 s per route on slower machines; budgets allow for that.
  timeout: 120_000,
  expect: { timeout: 30_000 },
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [['github'], ['html', { open: 'never' }]] : 'list',
  use: {
    baseURL: `http://localhost:${WEB_PORT}`,
    trace: 'retain-on-failure',
    // Locally use the installed Chrome; CI installs Playwright's Chromium.
    channel: process.env.CI ? undefined : 'chrome',
  },
  projects: [
    { name: 'desktop', use: { ...devices['Desktop Chrome'] } },
    { name: 'mobile', use: { ...devices['Pixel 7'] } },
  ],
  webServer: [
    {
      command: `rm -rf ${DATA_DIR} && .venv/bin/ai3d-server --port ${BACKEND_PORT}`,
      url: `http://127.0.0.1:${BACKEND_PORT}/api/v1/health`,
      env: {
        AI3D_DATA_DIR: DATA_DIR,
        AI3D_LOG_LEVEL: 'WARNING',
        // Registers the mock provider (placeholder output, never real generation).
        AI3D_ENVIRONMENT: 'test',
        // The local AI engine only when configured (see e2e/local-ai.spec.ts).
        AI3D_LOCAL_AI_DIR: process.env.AI3D_LOCAL_AI_DIR ?? 'test-results/no-local-ai',
        AI3D_OPENMVS_DIR: process.env.AI3D_OPENMVS_DIR ?? 'test-results/no-openmvs',
        AI3D_PROVIDER_POLL_INTERVAL: '0.3',
        // Local photogrammetry e2e runs only when COLMAP 4 is configured.
        ...(process.env.AI3D_COLMAP_PATH ? { AI3D_COLMAP_PATH: process.env.AI3D_COLMAP_PATH } : {}),
      },
      reuseExistingServer: false,
      timeout: 60_000,
    },
    {
      command: `pnpm --filter @ai3d/web exec next dev --port ${WEB_PORT}`,
      url: `http://localhost:${WEB_PORT}/projects`,
      env: { AI3D_API_URL: `http://127.0.0.1:${BACKEND_PORT}` },
      reuseExistingServer: false,
      timeout: 120_000,
    },
  ],
});
