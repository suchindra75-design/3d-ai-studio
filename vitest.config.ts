import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./vitest.setup.ts'],
    include: ['packages/*/src/**/*.test.{ts,tsx}', 'apps/*/src/**/*.test.{ts,tsx}'],
    css: false,
    // jsdom tests that render the studio took >5 s (Vitest's default) when the
    // machine was busy (load average 85 observed); 15 s still catches real hangs.
    testTimeout: 15_000,
  },
});
