import '@testing-library/jest-dom/vitest';
import { configure } from '@testing-library/react';

// Role queries in jsdom are slow when the whole suite runs in parallel; the
// default 1 s for findBy*/waitFor makes tests flaky on loaded machines.
configure({ asyncUtilTimeout: 5000 });

// jsdom does not implement ResizeObserver; Radix primitives (radio, switch) use it
// only to measure themselves, so a no-op is enough for behavioural tests.
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
globalThis.ResizeObserver ??= ResizeObserverStub as unknown as typeof ResizeObserver;
