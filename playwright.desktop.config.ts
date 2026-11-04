import { defineConfig, devices } from '@playwright/test';

// The desktop UI (apps/desktop, Vite) in a browser against a backend that, like
// the one the Tauri shell starts, refuses every API call without its token.
// The Tauri window itself cannot be automated on macOS (no WebDriver); the
// shell's Rust side is covered by `cargo test` (src-tauri/tests/sidecar.rs).
export const BACKEND_PORT = 8766;
const UI_PORT = 1766;
const DATA_DIR = 'test-results/e2e-desktop-data';
// A fixed, test-only value (the real app makes a random one per launch).
export const TEST_TOKEN = 'e2e-desktop-only-token-0123456789abcdef';

export default defineConfig({
  testDir: './e2e/desktop-shell',
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
  projects: [{ name: 'desktop-ui', use: { ...devices['Desktop Chrome'] } }],
  webServer: [
    {
      command: `rm -rf ${DATA_DIR} && .venv/bin/ai3d-server --port ${BACKEND_PORT}`,
      url: `http://127.0.0.1:${BACKEND_PORT}/api/v1/health`,
      env: {
        AI3D_DATA_DIR: DATA_DIR,
        AI3D_LOG_LEVEL: 'WARNING',
        AI3D_ENVIRONMENT: 'test', // mock provider: placeholder output, never real generation
        AI3D_PROVIDER_POLL_INTERVAL: '0.3',
        AI3D_API_TOKEN: TEST_TOKEN,
        AI3D_CORS_ORIGINS: JSON.stringify([`http://localhost:${UI_PORT}`]),
      },
      reuseExistingServer: false,
      timeout: 60_000,
    },
    {
      command: `pnpm --filter @ai3d/desktop exec vite --port ${UI_PORT} --strictPort`,
      url: `http://localhost:${UI_PORT}`,
      env: {
        VITE_AI3D_API_URL: `http://127.0.0.1:${BACKEND_PORT}`,
        VITE_AI3D_API_TOKEN: TEST_TOKEN,
      },
      reuseExistingServer: false,
      timeout: 60_000,
    },
  ],
});
