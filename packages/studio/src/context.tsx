'use client';

import type { ApiClient } from '@ai3d/api-client';
import { createContext, useContext, type ComponentType, type ReactNode } from 'react';

export interface StudioLinkProps {
  href: string;
  className?: string;
  children: ReactNode;
  'aria-current'?: 'page' | undefined;
  'aria-label'?: string;
}

/** How shared screens navigate; each app supplies its router's implementation. */
export interface StudioNavigation {
  Link: ComponentType<StudioLinkProps>;
  push: (href: string) => void;
  replace: (href: string) => void;
  /** React hook returning the current pathname. */
  usePathname: () => string;
}

export interface RuntimeFact {
  label: string;
  value: string;
}

/** What only the desktop app can do (native dialogs, its own data folder). */
export interface DesktopFeatures {
  /** Asks where to save a backend download; resolves to the saved path, or null if cancelled. */
  saveFile: (path: string, suggestedName: string) => Promise<string | null>;
  openDataFolder: () => Promise<void>;
}

export interface StudioContextValue {
  api: ApiClient;
  navigation: StudioNavigation;
  platform: 'web' | 'desktop';
  desktop?: DesktopFeatures;
  /** Platform facts shown on the About screen (e.g. desktop shell version). */
  runtime?: RuntimeFact[];
}

const StudioContext = createContext<StudioContextValue | null>(null);

export function StudioProvider({
  value,
  children,
}: {
  value: StudioContextValue;
  children: ReactNode;
}) {
  return <StudioContext.Provider value={value}>{children}</StudioContext.Provider>;
}

export function useStudio(): StudioContextValue {
  const value = useContext(StudioContext);
  if (!value) throw new Error('useStudio must be used inside <StudioProvider>');
  return value;
}

export function useApi(): ApiClient {
  return useStudio().api;
}
