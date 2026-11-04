'use client';

import { createApiClient } from '@ai3d/api-client';
import {
  StudioProvider,
  createQueryClient,
  type StudioContextValue,
  type StudioLinkProps,
} from '@ai3d/studio';
import { QueryClientProvider } from '@tanstack/react-query';
import NextLink from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import { useMemo, useState, type ReactNode } from 'react';

function Link({ href, ...props }: StudioLinkProps) {
  return <NextLink href={href} {...props} />;
}

function useNextPathname(): string {
  return usePathname() ?? '/';
}

// Same origin: Next.js rewrites /api/v1/* to the backend (see next.config.ts).
const api = createApiClient({ baseUrl: '' });

export function Providers({ children }: { children: ReactNode }) {
  const [queryClient] = useState(createQueryClient);
  const router = useRouter();
  const value = useMemo<StudioContextValue>(
    () => ({
      api,
      platform: 'web',
      navigation: {
        Link,
        push: (href) => router.push(href),
        replace: (href) => router.replace(href),
        usePathname: useNextPathname,
      },
    }),
    [router],
  );
  return (
    <QueryClientProvider client={queryClient}>
      <StudioProvider value={value}>{children}</StudioProvider>
    </QueryClientProvider>
  );
}
