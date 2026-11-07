import type { ApiClient } from '@ai3d/api-client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render } from '@testing-library/react';
import type { ReactElement } from 'react';

import { StudioProvider, type StudioLinkProps } from '../context';

type DeepPartial<T> = {
  [K in keyof T]?: T[K] extends (...args: never[]) => unknown ? T[K] : DeepPartial<T[K]>;
};

export function fakeApi(overrides: DeepPartial<ApiClient> = {}): ApiClient {
  const base = {
    baseUrl: 'http://api',
    url: (path: string) => `http://api${path}`,
    health: vi.fn().mockResolvedValue({ status: 'ok', version: '0.1.0' }),
    systemInfo: vi.fn(),
    projects: {
      list: vi.fn(),
      get: vi.fn(),
      create: vi.fn(),
      update: vi.fn(),
      delete: vi.fn(),
      captureAssessment: vi.fn(),
      measureOverlap: vi.fn(),
    },
    images: { list: vi.fn(), get: vi.fn(), delete: vi.fn(), upload: vi.fn() },
    devices: { local: vi.fn() },
    providers: { list: vi.fn(), setApiKey: vi.fn(), deleteApiKey: vi.fn(), check: vi.fn() },
    generations: { plan: vi.fn(), create: vi.fn() },
    jobs: {
      list: vi.fn(),
      get: vi.fn(),
      events: vi.fn(),
      streamUrl: (id: string) => `http://api/api/v1/jobs/${id}/stream`,
      cancel: vi.fn(),
      consent: vi.fn(),
      retry: vi.fn(),
    },
    models: {
      list: vi.fn(),
      get: vi.fn(),
      delete: vi.fn(),
      update: vi.fn(),
      versions: vi.fn(),
      saveVersion: vi.fn(),
      enhancePlan: vi.fn(),
      enhance: vi.fn(),
    },
    exports: { formats: vi.fn(), list: vi.fn(), create: vi.fn(), delete: vi.fn() },
    lod: {
      get: vi.fn().mockResolvedValue({ status: 'not_needed', triangles: 0, levels: [] }),
      build: vi.fn(),
    },
    inspection: {
      get: vi.fn().mockResolvedValue({ status: 'missing' }),
      start: vi.fn(),
    },
    repairs: { create: vi.fn() },
  };
  return {
    ...base,
    ...overrides,
    projects: { ...base.projects, ...overrides.projects },
    images: { ...base.images, ...overrides.images },
    devices: { ...base.devices, ...overrides.devices },
    providers: { ...base.providers, ...overrides.providers },
    generations: { ...base.generations, ...overrides.generations },
    jobs: { ...base.jobs, ...overrides.jobs },
    models: { ...base.models, ...overrides.models },
    exports: { ...base.exports, ...overrides.exports },
    lod: { ...base.lod, ...overrides.lod },
    inspection: { ...base.inspection, ...overrides.inspection },
    repairs: { ...base.repairs, ...overrides.repairs },
  } as unknown as ApiClient;
}

function Link({ href, children, ...props }: StudioLinkProps) {
  return (
    <a href={href} {...props}>
      {children}
    </a>
  );
}

export function renderWithStudio(ui: ReactElement, api: ApiClient = fakeApi()) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const navigation = { Link, push: vi.fn(), replace: vi.fn(), usePathname: () => '/projects' };
  const result = render(
    <QueryClientProvider client={queryClient}>
      <StudioProvider value={{ api, navigation, platform: 'web' }}>{ui}</StudioProvider>
    </QueryClientProvider>,
  );
  return { ...result, api, navigation, queryClient };
}
