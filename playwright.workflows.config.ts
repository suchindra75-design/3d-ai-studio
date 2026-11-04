import { defineConfig, devices } from '@playwright/test';

// Job workflows that need the test-only mock provider to behave differently
// from the main suite: tasks take ~12 s (time to cancel one) and the first task
// for each set of photos fails, so retrying it succeeds. Runs the studio UI
// (apps/desktop's Vite build, same screens as the web app) against its own
// backend. Nothing here contacts a real AI service.
const BACKEND_PORT = 8767;
const UI_PORT = 1767;
const DATA_DIR = 'test-results/e2e-workflows-data';

export default defineConfig({
  testDir: './e2e/workflows',
  timeout: 120_000,
  expect: { timeout: 30_000 },
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [['github'], ['html', { open: 'never' }]] : 'list',
  use: {
    baseURL: `http://localhost:${UI_PORT}`,
    trace: 'retain-on-failure',
    channel: process.env.CI ? undefined : 'chrome',
  },
  projects: [{ name: 'workflows', use: { ...devices['Desktop Chrome'] } }],
  webServer: [
    {
      command: `rm -rf ${DATA_DIR} && .venv/bin/ai3d-server --port ${BACKEND_PORT}`,
      url: `http://127.0.0.1:${BACKEND_PORT}/api/v1/health`,
      env: {
        AI3D_DATA_DIR: DATA_DIR,
        AI3D_LOG_LEVEL: 'WARNING',
        AI3D_ENVIRONMENT: 'test',
        // The local AI engine only when configured (see e2e/local-ai.spec.ts).
        AI3D_LOCAL_AI_DIR: process.env.AI3D_LOCAL_AI_DIR ?? 'test-results/no-local-ai',
        AI3D_PROVIDER_POLL_INTERVAL: '0.3',
        AI3D_MOCK_POLLS_UNTIL_DONE: '40',
        AI3D_MOCK_FAIL_FIRST_TASK: '1',
        AI3D_CORS_ORIGINS: JSON.stringify([`http://localhost:${UI_PORT}`]),
      },
      reuseExistingServer: false,
      timeout: 60_000,
    },
    {
      command: `pnpm --filter @ai3d/desktop exec vite --port ${UI_PORT} --strictPort`,
      url: `http://localhost:${UI_PORT}`,
      env: { VITE_AI3D_API_URL: `http://127.0.0.1:${BACKEND_PORT}` },
      reuseExistingServer: false,
      timeout: 60_000,
    },
  ],
});
